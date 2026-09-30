"""运营月报 CSV 的门急诊人次、住院人次在库里按机构分组数（P2-1150，第三十三批扫描 A4-2）。

`GET /api/reports/operations/export` 原先用两句 `.all()` 把全部就诊、全部住院的（机构, 时刻）读进内存，再对每家机构把整张表
扫一遍、逐行比 `strftime("%Y-%m") == period`——耗时按「机构数 × 全部就诊」涨，period 也没下推到库里：29 家机构、就诊 6 万时
一次取回 62,131 行、约 1.2 s（扫描实测；300 万就诊 × 30 家外推约 60 s，正好卡在 nginx 缺省的超时上）。同一个函数里的收支
早就在库里按期间过滤、GROUP BY org_id。

修后就诊、住院各一条 `GROUP BY org_id`，给了 period 就加 `[月初, 次月初)`——与原先的 `month_of` 同一个口径：落库的 naive UTC
时刻所在的月份；不带 period 照旧是累计。

- 特征化：月界前后一微秒、住院类就诊、另一家机构、没有任何业务的机构，逐格与原算法相同（不带 period、2026-06 / 07 / 08）——
  改前改后都绿；
- 缺陷：就诊、住院各多灌一批（期间内外都有），请求取回的行数不变——修前每条就诊、住院各取回一行。
"""
import csv
import io
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import Admission, Bed, Encounter, Organization, Patient, User, Ward

#: （机构键, 就诊类型, 时刻）
ENCOUNTERS = [
    ("a", "outpatient", datetime(2025, 1, 1, 9, 0)),                 # 历史
    ("a", "outpatient", datetime(2026, 6, 30, 23, 59, 59, 999999)),  # 六月最后一微秒
    ("a", "outpatient", datetime(2026, 7, 1, 0, 0)),                 # 七月第一刻
    ("a", "outpatient", datetime(2026, 7, 31, 23, 59, 59, 999999)),
    ("a", "outpatient", datetime(2026, 8, 1, 0, 0)),
    ("a", "inpatient", datetime(2026, 7, 15, 10, 0)),                # 入院建的住院类就诊：不算门急诊（P2-153）
    ("b", "outpatient", datetime(2026, 7, 10, 20, 0)),               # UTC 晚上 8 点（东八区已是次日）：按落库的 UTC 月
    ("b", "emergency", datetime(2026, 8, 2, 8, 0)),
]

#: （机构键, 入院时刻）
ADMISSIONS = [
    ("a", datetime(2026, 6, 30, 23, 59, 59, 999999)),
    ("a", datetime(2026, 7, 1, 0, 0)),
    ("a", datetime(2026, 7, 20, 9, 0)),
    ("b", datetime(2026, 7, 31, 23, 59, 59, 999999)),
    ("b", datetime(2026, 8, 1, 0, 0)),
]


def _seed(db, world, encounters, admissions):
    for key, kind, at in encounters:
        db.add(Encounter(patient_id=world["patient"], org_id=world[key], encounter_type=kind, created_at=at))
    for key, at in admissions:
        db.add(Admission(patient_id=world["patient"], org_id=world[key], ward_id=world[f"{key}_ward"],
                         bed_id=world[f"{key}_bed"], status="discharged", admitted_at=at,
                         discharged_at=at + timedelta(days=3), created_by=world["user"]))
    db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    """直接落库造数：月界要精确到微秒，走接口做不到。机构丙什么业务都没有（导出里照样一行，两列都是 0）。"""
    with SessionLocal() as db:
        orgs = {key: Organization(name=f"运营月报{name}", org_type=kind, level=level) for key, name, kind, level in (
            ("a", "甲院", "lead_hospital", "county"), ("b", "乙院", "township", "township"),
            ("c", "丙站", "village", "village"))}
        patient = Patient(ehc_no="EHC-OPS-0001", name="运营月报患者", id_card="330382198808081234")
        db.add_all([*orgs.values(), patient])
        db.flush()
        ids = {key: org.id for key, org in orgs.items()}
        ids["patient"] = patient.id
        ids["user"] = db.query(User.id).filter(User.username == "admin").scalar()
        for key in ("a", "b"):
            ward = Ward(org_id=ids[key], name=f"运营月报病区{key}")
            db.add(ward)
            db.flush()
            bed = Bed(ward_id=ward.id, bed_no=f"OPS-{key}")
            db.add(bed)
            db.flush()
            ids[f"{key}_ward"], ids[f"{key}_bed"] = ward.id, bed.id
        _seed(db, ids, ENCOUNTERS, ADMISSIONS)
    return ids


def _原算法(db, period: str | None) -> dict[int, tuple[int, int]]:
    """修前的算法原样搬来当判据：全部就诊、住院读出来，逐家机构整表扫、逐行比 UTC 月份。"""

    def month_of(dt) -> str:
        return dt.strftime("%Y-%m") if dt else ""

    encounters = (
        db.query(Encounter.org_id, Encounter.created_at).filter(Encounter.encounter_type != "inpatient").all()
    )
    admissions = db.query(Admission.org_id, Admission.admitted_at).all()
    return {
        org_id: (
            sum(1 for oid, at in encounters if oid == org_id and (period is None or month_of(at) == period)),
            sum(1 for oid, at in admissions if oid == org_id and (period is None or month_of(at) == period)),
        )
        for (org_id,) in db.query(Organization.id).order_by(Organization.id)
    }


def _export(client, admin, period: str | None) -> list[list[str]]:
    url = "/api/reports/operations/export" + (f"?period={period}" if period else "")
    resp = client.get(url, headers=admin)
    assert resp.status_code == 200, resp.text
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))


@pytest.mark.parametrize("period", [None, "2026-06", "2026-07", "2026-08"])
def test_特征化_两列逐格与原算法相同(client, admin, world, period):
    rows = _export(client, admin, period)
    assert rows[0][3:5] == ["门急诊人次", "住院人次"]
    got = {int(r[0]): (int(r[3]), int(r[4])) for r in rows[1:]}
    with SessionLocal() as db:
        assert got == _原算法(db, period)


def test_特征化_七月逐格(client, admin, world):
    got = {int(r[0]): (r[3], r[4]) for r in _export(client, admin, "2026-07")[1:]}
    assert got[world["a"]] == ("2", "2")   # 七月第一刻与最后一微秒都算；六月最后一微秒、住院类就诊不算
    assert got[world["b"]] == ("1", "1")
    assert got[world["c"]] == ("0", "0")
    everything = {int(r[0]): (r[3], r[4]) for r in _export(client, admin, None)[1:]}
    assert everything[world["a"]] == ("5", "3") and everything[world["b"]] == ("2", "2")


def _rows_fetched(client, admin, url: str) -> int:
    """这一个请求里全部 SELECT 取回的行数：拦下每条语句与参数，请求结束后在同一份数据上重放一遍数行数。"""
    seen: list[tuple] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get(url, headers=admin)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text
    assert seen, "没拦到任何 SELECT（用例失效，请检查拦截方式）"
    with engine.connect() as conn:
        return sum(len(conn.exec_driver_sql(statement, parameters).fetchall()) for statement, parameters in seen)


@pytest.mark.parametrize("period", [None, "2026-07"])
def test_就诊住院翻几番_取回行数不变(client, admin, world, period):
    url = "/api/reports/operations/export" + (f"?period={period}" if period else "")
    before = _rows_fetched(client, admin, url)
    more = 40
    moments = [datetime(2026, 6, 16, 9, 0) + timedelta(days=i) for i in range(more)]   # 六月下旬到七月下旬
    with SessionLocal() as db:   # 两家已有业务的机构、期间内外都有：只加量，不加任何一个分组
        _seed(db, world, [(key, "outpatient", at) for key in ("a", "b") for at in moments],
              [(key, at) for key in ("a", "b") for at in moments])
    after = _rows_fetched(client, admin, url)
    assert after == before, (
        f"两家机构各多了 {more} 条就诊、{more} 条住院，请求多取回了 {after - before} 行——还在把就诊、住院整表读进内存数"
    )
