"""驾驶舱「近 N 月业务量趋势」在库里按年月聚合、只数窗口以内（P2-1149，第三十三批扫描 A4-1）。

`GET /api/metrics/trends` 是驾驶舱的一块，驾驶舱是管理端所有账号登录后的首页。原先对就诊、检查报告、转诊、处方四张表
各 `db.query(时间列).all()`——不带任何时间下界，把建库以来的全部时间戳读进内存，再在 Python 里 Counter 分桶，而窗口
只要最近 6～24 个月：就诊 2 万 → 6 万时一次取回 25,001 → 65,001 行（扫描实测，县域外推 500 万行约 28 s、1.3 GB 一次）。

修后四条序列各一条 `GROUP BY 年, 月`，加「≥ 窗口首月 1 日零点」的下界。分桶口径不变：仍按落库的 naive UTC 时间戳的
年月分（UTC 月末最后那几个小时东八区已是次月，照旧算在 UTC 的当月），月份键仍按本地业务日期往回数。

- 特征化：边界时刻（窗口首月 1 日零点与前一月最后一微秒、跨年、窗口之后的未来月、住院类就诊）逐月与原算法相同
  （months=6、24 两种）——改前改后都绿；
- 缺陷：灌一批窗口以外的历史行，请求取回的行数不变——修前多出整批历史；
- 真 PG 档：`extract` 在 PG 上回的是 numeric、分组表达式要与选择列一字不差，SQLite 绿了不等于 PG 也对（CLAUDE.md §6）。
"""
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from conftest import freeze_business_date

from app.database import SessionLocal, engine
from app.models import Encounter, ExamReport, ExamRequest, Organization, Patient, Prescription, Referral, User
from app.routers.metrics import OUTPATIENT_ENCOUNTER, monthly_trends

#: 冻住的业务日期：months=6 的窗口是 2025-10～2026-03，months=24 的是 2024-04～2026-03
TODAY = date(2026, 3, 10)

#: 四张表各落一行的时刻。月末最后一微秒那两行还顺带钉住 SQLite 的日期函数：它用浮点儒略日、只到毫秒，
#: `strftime` 会把这两个时刻算进次月、次年（`deps.utc_date_parts` 的 docstring），原算法按 Python 读回的值算在当月
MOMENTS = [
    datetime(2019, 6, 15, 8, 0),                  # 两个窗口以外的历史
    datetime(2024, 3, 31, 23, 59, 59, 999999),    # 24 个月窗口首月的前一微秒：不算
    datetime(2024, 4, 1, 0, 0),                   # 24 个月窗口首月 1 日零点：算进首月
    datetime(2025, 9, 30, 23, 59, 59, 999999),    # 6 个月窗口首月的前一微秒；24 个月窗口里算九月
    datetime(2025, 10, 1, 0, 0),                  # 6 个月窗口首月 1 日零点
    datetime(2025, 12, 31, 23, 59, 59, 999999),   # 跨年的前一微秒：算十二月
    datetime(2026, 1, 1, 0, 0),
    datetime(2026, 2, 28, 20, 0),                 # UTC 二月最后一晚（东八区已是 3 月 1 日）：按落库的 UTC 算二月
    datetime(2026, 3, 10, 9, 30),                 # 当月
    datetime(2026, 4, 2, 10, 0),                  # 窗口之后的未来月：不在月份键里
]

#: 缺陷用例每轮灌进去的窗口外历史（每张表各这么多行）
HISTORY = 60


def _seed(db, moments, org_id, patient_id, user_id):
    """每个时刻四张表各一行；另在同一时刻落一条住院类就诊（趋势不数它，P2-642）。"""
    for at in moments:
        db.add(Encounter(patient_id=patient_id, org_id=org_id, encounter_type="outpatient", created_at=at))
        db.add(Encounter(patient_id=patient_id, org_id=org_id, encounter_type="inpatient", created_at=at))
        request = ExamRequest(patient_id=patient_id, from_org_id=org_id, center_type="imaging", item_code="CT",
                              item_name="胸部CT", created_by=user_id)
        db.add(request)
        db.flush()
        db.add(ExamReport(request_id=request.id, conclusion="未见异常", reported_at=at))
        db.add(Referral(patient_id=patient_id, from_org_id=org_id, to_org_id=org_id, direction="up",
                        created_by=user_id, created_at=at))
        db.add(Prescription(patient_id=patient_id, org_id=org_id, created_by=user_id, created_at=at))
    db.commit()


def _原算法(db, keys: list[str]) -> dict[str, list[int]]:
    """修前 `monthly_trends` 的算法原样搬来当判据：时间戳全部读出来，按 naive UTC 的年月 Counter 分桶。"""
    series = {}
    for name, column, where in (
        ("encounters", Encounter.created_at, OUTPATIENT_ENCOUNTER),
        ("exam_reports", ExamReport.reported_at, None),
        ("referrals", Referral.created_at, None),
        ("prescriptions", Prescription.created_at, None),
    ):
        query = db.query(column) if where is None else db.query(column).filter(where)
        counter = Counter(f"{row[0].year:04d}-{row[0].month:02d}" for row in query.all() if row[0])
        series[name] = [counter.get(k, 0) for k in keys]
    return series


@pytest.fixture(scope="module")
def world(client, admin):
    """直接落库造数：要精确控制跨月时间戳，走接口做不到。"""
    with SessionLocal() as db:
        org = Organization(name="趋势窗口县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-TREND-0001", name="趋势窗口患者", id_card="330382199303031234")
        db.add_all([org, patient])
        db.flush()
        user_id = db.query(User.id).filter(User.username == "admin").scalar()
        ids = {"org": org.id, "patient": patient.id, "user": user_id}
        _seed(db, MOMENTS, ids["org"], ids["patient"], ids["user"])
    return ids


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


@pytest.mark.parametrize("months, first", [(6, "2025-10"), (24, "2024-04")])
def test_特征化_逐月与原算法相同(client, admin, world, months, first):
    with freeze_business_date(TODAY):
        resp = client.get(f"/api/metrics/trends?months={months}", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["months"]) == months and (body["months"][0], body["months"][-1]) == (first, "2026-03")
    with SessionLocal() as db:
        assert body["series"] == _原算法(db, body["months"])
    # 非空洞：首月 1 日零点那条算进首月，前一月最后一微秒那条不算；住院类就诊不数；UTC 二月最后一晚算二月
    for values in body["series"].values():
        assert values[0] == 1
        assert values[-6:] == [1, 0, 1, 1, 1, 1]   # 2025-10 … 2026-03


@pytest.mark.parametrize("months", [6, 24])
def test_窗口以外的历史行不再读出来(client, admin, world, months):
    url = f"/api/metrics/trends?months={months}"
    with freeze_business_date(TODAY):
        expected = client.get(url, headers=admin).json()
        before = _rows_fetched(client, admin, url)
        with SessionLocal() as db:
            _seed(db, [datetime(2015, 1, 1, 8) + timedelta(days=i) for i in range(HISTORY)],
                  world["org"], world["patient"], world["user"])
        after = _rows_fetched(client, admin, url)
        assert client.get(url, headers=admin).json() == expected   # 窗口以外的行不改变任何一格
    assert after == before, (
        f"四张表各灌了 {HISTORY} 行窗口以外的历史，请求多取回了 {after - before} 行——时间戳还在整表读进内存分桶"
    )


# ------------------------------------------------------------------ 真 PG 档（默认跳过）

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL")
def test_真PG_按年月分组与原算法逐月相同():
    """PG 的 `EXTRACT` 回 numeric、分组表达式须与选择列一致——换库才会现形的两处。

    **这套库是多人共用的**：只用带随机后缀的自建数据，判据与被测在同一个会话上现算（库里别人的行两边一样多），
    跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
    """
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import sessionmaker

    pg = create_engine(PG_URL)
    if not sa_inspect(pg).has_table("encounters"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    Session = sessionmaker(bind=pg, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG趋势院{tag}", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no=f"PG-TREND-{tag}", name=f"PG趋势患者{tag}",
                          id_card=f"3399{uuid.uuid4().int % 10**14:014d}")
        user = User(username=f"pg_trend_{tag}", password_hash="x", full_name="趋势经办", role="doctor")
        db.add_all([org, patient, user])
        db.flush()
        ids = {"org": org.id, "patient": patient.id, "user": user.id}
        _seed(db, MOMENTS, ids["org"], ids["patient"], ids["user"])
    try:
        with Session() as db, freeze_business_date(TODAY):
            for months in (6, 24):
                body = monthly_trends(months=months, db=db)
                assert body["series"] == _原算法(db, body["months"]), months
                assert all(values[0] >= 1 for values in body["series"].values())   # 首月 1 日零点那条在
    finally:
        with Session() as db:
            request_ids = [rid for (rid,) in db.query(ExamRequest.id).filter(ExamRequest.patient_id == ids["patient"])]
            db.query(ExamReport).filter(ExamReport.request_id.in_(request_ids)).delete(synchronize_session=False)
            for model in (ExamRequest, Encounter, Referral, Prescription):
                db.query(model).filter(model.patient_id == ids["patient"]).delete(synchronize_session=False)
            db.query(Patient).filter(Patient.id == ids["patient"]).delete(synchronize_session=False)
            db.query(User).filter(User.id == ids["user"]).delete(synchronize_session=False)
            db.query(Organization).filter(Organization.id == ids["org"]).delete(synchronize_session=False)
            db.commit()
        pg.dispose()
