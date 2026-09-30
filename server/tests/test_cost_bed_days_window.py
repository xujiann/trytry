"""诊次 / 床日成本的占用床日只读与统计期有交集的住院（P2-1152，第三十三批扫描 A4-8）。

`cost._occupied_bed_days`（`GET /api/cost/unit-cost` 的床日分母）原先只有「入院 < 期末」一个条件：为算一个月的床日，把这家机构
建库以来的全部住院整行读进内存——住院 2 万 → 6 万时载入 20,002 → 60,002 行、215 → 1,416 ms、峰值 34 → 102 MB（扫描实测）。
出院早于期初的住院与统计期没有交集，函数里本来就按 `max(…, 0)` 计 0 床日（当日入出院的 1 天也只给落在期内的，P2-135）。

修后补「出院为空或 ≥ 期初零点」这个下界（运行效率 `analytics._efficiency_rows` 早就是这个过滤），床日逐月不变。

- 特征化：六个月份（含闰二月、跨年）逐月与原算法相同——期初零点前一微秒出院、期初零点出院、期初零点入出（下界含期初）、
  当日入出院、在院未出院、跨月住院、出院早于入院的脏数据——改前改后都绿；
- 缺陷：多灌一批期前早已出院的历史住院，算一个月载入的住院行数不变（修前一条历史一行）。
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event

from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, User, Ward
from app.routers.cost import _occupied_bed_days

#: （入院, 出院；None 为在院）
STAYS = [
    (datetime(2024, 1, 10, 8, 0), datetime(2024, 1, 31, 23, 59, 59, 999999)),   # 二月期初零点前一微秒出院：二月 0
    (datetime(2024, 1, 20, 9, 0), datetime(2024, 2, 1, 0, 0)),                   # 二月期初零点出院：二月 0
    (datetime(2024, 2, 29, 8, 0), datetime(2024, 2, 29, 17, 0)),                 # 闰日当日入出院：二月 1
    (datetime(2024, 2, 28, 20, 0), datetime(2024, 3, 2, 10, 0)),                 # 跨月：二月 2、三月 1
    (datetime(2025, 12, 30, 9, 0), datetime(2026, 1, 2, 9, 0)),                  # 跨年：十二月 2、一月 1
    (datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 0)),                    # 期初零点入、零点出：一月 1（下界含期初）
    (datetime(2026, 5, 15, 9, 0), None),                                         # 在院：按期末计
    (datetime(2026, 6, 30, 23, 0), datetime(2026, 7, 1, 0, 0)),                  # 住了一晚：六月 1、七月 0
    (datetime(2026, 7, 10, 10, 0), datetime(2026, 7, 5, 10, 0)),                 # 出院早于入院的脏数据：0
    (datetime(2019, 3, 1, 8, 0), datetime(2019, 3, 20, 8, 0)),                   # 历史
]

#: 逐月核对的统计期：[期初, 期末)
MONTHS = [
    (date(2024, 2, 1), date(2024, 3, 1)),
    (date(2024, 3, 1), date(2024, 4, 1)),
    (date(2025, 12, 1), date(2026, 1, 1)),
    (date(2026, 1, 1), date(2026, 2, 1)),
    (date(2026, 6, 1), date(2026, 7, 1)),
    (date(2026, 7, 1), date(2026, 8, 1)),
]


def _add(db, world, stays):
    for admitted, discharged in stays:
        db.add(Admission(patient_id=world["patient"], org_id=world["org"], ward_id=world["ward"], bed_id=world["bed"],
                         status="discharged" if discharged else "admitted", admitted_at=admitted,
                         discharged_at=discharged, created_by=world["user"]))
    db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    """在院的只有一条（同一患者同时只能有一条在院记录，库里有部分唯一索引）。"""
    with SessionLocal() as db:
        org = Organization(name="床日下界县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-BEDDAY-0001", name="床日下界患者", id_card="330382196606061234")
        db.add_all([org, patient])
        db.flush()
        ward = Ward(org_id=org.id, name="床日下界病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="BD-1")
        db.add(bed)
        db.flush()
        ids = {"org": org.id, "patient": patient.id, "ward": ward.id, "bed": bed.id,
               "user": db.query(User.id).filter(User.username == "admin").scalar()}
        _add(db, ids, STAYS)
    return ids


def _原算法(db, org_id: int, start: date, end: date) -> int:
    """修前的 `_occupied_bed_days` 原样搬来当判据：只有「入院 < 期末」一个条件。"""
    total = 0
    for adm in db.query(Admission).filter(
        Admission.org_id == org_id, Admission.admitted_at < datetime.combine(end, datetime.min.time())
    ).all():
        admitted = adm.admitted_at.date()
        discharged = adm.discharged_at.date() if adm.discharged_at else end
        if admitted == discharged:
            total += 1 if start <= admitted < end else 0
            continue
        total += max((min(discharged, end) - max(admitted, start)).days, 0)
    return total


def _bed_days(db, org_id: int, start: date, end: date) -> tuple[int, int]:
    """（床日, 这一次载入的住院 ORM 对象个数）"""
    loaded = [0]

    def on_load(target, context):
        loaded[0] += 1

    event.listen(Admission, "load", on_load)
    try:
        days = _occupied_bed_days(db, org_id, start, end)
    finally:
        event.remove(Admission, "load", on_load)
    return days, loaded[0]


@pytest.mark.parametrize("start, end", MONTHS)
def test_特征化_逐月床日与原算法相同(world, start, end):
    with SessionLocal() as db:
        assert _occupied_bed_days(db, world["org"], start, end) == _原算法(db, world["org"], start, end)


def test_特征化_逐月床日明细(world):
    with SessionLocal() as db:
        got = [_occupied_bed_days(db, world["org"], start, end) for start, end in MONTHS]
    # 二月：闰日当日入出 1 + 跨月 2；三月：跨月 1；十二月：跨年 2；一月：跨年 1 + 期初零点入出 1；
    # 六月：在院 30 + 住一晚 1；七月：在院整月
    assert got == [3, 1, 2, 2, 31, 31]


def test_特征化_接口的床日同一个数(client, admin, world):
    body = client.get(f"/api/cost/unit-cost?period=2024-02&org_id={world['org']}", headers=admin).json()
    assert body["occupied_bed_days"] == 3


def test_期前早已出院的历史住院不再载入(world):
    july = (date(2026, 7, 1), date(2026, 8, 1))
    with SessionLocal() as db:
        before_days, before_loaded = _bed_days(db, world["org"], *july)
    with SessionLocal() as db:
        _add(db, world, [(datetime(2018, 1, 1, 8, 0) + timedelta(days=i), datetime(2018, 1, 3, 8, 0) + timedelta(days=i))
                         for i in range(80)])
    with SessionLocal() as db:
        after_days, after_loaded = _bed_days(db, world["org"], *july)
    assert after_days == before_days
    assert after_loaded == before_loaded, (
        f"多了 80 条期前早已出院的住院，算七月的床日多载入了 {after_loaded - before_loaded} 行——还在读建库以来的全部住院"
    )
