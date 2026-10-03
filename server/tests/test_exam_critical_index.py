"""危急值判据用得上只收危急值的部分索引（P2-1156，第三十三批扫描 A4-11）。

修前：待办铃铛（每人每 30 s 轮询）、驾驶舱、运营报表、危急值清单与超时催办的判据都是
`critical IS TRUE AND critical_status IN (…, '')`。`critical` 没有索引；`critical_status` 有，但普通报告缺省就是 ''、
恰在 IN 列表里，这个索引一条也筛不掉——每次按报告总量逐条回表（报告 4.4 万、危急值 30 条时每条语句约 5 ms）。只补
索引也不够：ORM 的 `.is_(True)` 编译成 `IS 1`（SQLite）/ `IS true`（PG），两个库都认不出它蕴含索引谓词。修后迁移补
`exam_reports(critical)` 只收危急值的部分索引，判据各处一律写成 `ExamReport.critical == true()`。

- `test_结果不变_*`：各接口的计数与清单照旧（修前修后都绿）；
- `test_用得上部分索引_SQLite`：各接口实际发出的危急值语句逐条 EXPLAIN QUERY PLAN，都走 `ix_exam_reports_critical`
  （修前红）；
- `test_判据一处也不许写回_is_`：`app/` 下不许再出现 `ExamReport.critical.is_(…)`（AST 扫描）；
- `test_用得上部分索引_PG`：真 PG（integration，缺服务跳过）上迁移建出了这个部分索引、判据用得上它。
"""
import ast
import os
import pathlib
import re
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select, text, true

from conftest import business_today, login

from app.database import SessionLocal, engine
from app.models import ExamReport, ExamRequest, Organization, Patient, User

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")
APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
#: 认索引名本身，别把 `ix_exam_reports_critical_status` 也认成它（`\b` 在 `critical` 与 `_` 之间不成立）
INDEX = re.compile(r"\bix_exam_reports_critical\b")


@pytest.fixture(scope="module")
def world(client, admin):
    """两家卫生院；普通报告若干，危急值五条：甲院已通知 / 已确认 / 已处置，乙院存量空状态 / 已通知。"""
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        orgs = [Organization(name=f"P21156 {tag}卫生院", org_type="township", level="township") for tag in "甲乙"]
        db.add_all(orgs)
        db.flush()
        patient = Patient(name="P21156 患者", id_card="330127199001021156", ehc_no="EHC-P21156")
        db.add(patient)
        db.flush()
        cases = [(0, False, "")] * 6 + [
            (0, True, "notified"), (0, True, "acknowledged"), (0, True, "resolved"),
            (1, True, ""), (1, True, "notified"),
        ]
        ids = []
        for org_index, critical, status in cases:
            request = ExamRequest(patient_id=patient.id, from_org_id=orgs[org_index].id, center_type="lab",
                                  item_code="K", item_name="血钾", status="reported", created_by=creator)
            db.add(request)
            db.flush()
            report = ExamReport(request_id=request.id, conclusion="结论", critical=critical, critical_status=status)
            db.add(report)
            db.flush()
            ids.append(report.id)
        db.commit()
        org_ids = [o.id for o in orgs]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p21156_doc", "password": "passw0rd1", "role": "doctor", "org_id": org_ids[0], "full_name": "甲院医生"})
    assert doctor.status_code in (200, 201), doctor.text
    return {"ids": ids, "doctor": login(client, "p21156_doc", "passw0rd1")}


def _todos(client, headers) -> dict:
    return {item["type"]: item for item in client.get("/api/todos", headers=headers).json()["items"]}


def _calls(admin, doctor) -> list[tuple[str, str, dict]]:
    """判据所在的各条路：待办铃铛两节（管理员 / 院长、医生）、驾驶舱下钻与总览、运营报表、危急值清单、超时催办。"""
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    return [("待办·管理员", "/api/todos", admin), ("待办·医生", "/api/todos", doctor)] + [
        (path, path, admin) for path in (
            "/api/metrics/drilldown?metric=critical_values", "/api/metrics/overview", "/api/reports/monitoring",
            "/api/exams/critical", f"/api/exams/critical/unacknowledged?today={tomorrow}")
    ]


def test_结果不变_待办铃铛(client, admin, world):
    ids = world["ids"]
    assert _todos(client, admin)["critical_report"]["count"] == 4   # 甲院已通知 / 已确认、乙院空状态 / 已通知
    assert [r["id"] for r in _todos(client, admin)["critical_report"]["list"]] == [ids[10], ids[9], ids[7], ids[6]]
    ack = _todos(client, world["doctor"])["critical_ack"]   # 医生只看本院申请单上待确认的
    assert (ack["count"], [r["id"] for r in ack["list"]]) == (1, [ids[6]])


def test_结果不变_驾驶舱_报表_清单与催办(client, admin, world):
    ids = world["ids"]
    drill = client.get("/api/metrics/drilldown?metric=critical_values", headers=admin).json()
    assert drill["total"] == 4
    assert client.get("/api/metrics/overview", headers=admin).json()["remote_diagnosis"]["critical_values"] == 4
    monitoring = client.get("/api/reports/monitoring", headers=admin).json()["indicators"]
    assert [i["value"] for i in monitoring if i["name"] == "危急值未闭环例数"] == [4]
    listed = client.get("/api/exams/critical", headers=admin).json()
    assert [r["id"] for r in listed] == [ids[10], ids[9], ids[7], ids[6], ids[8]]   # 没处置完的在前，已处置的垫底
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    unacked = client.get(f"/api/exams/critical/unacknowledged?today={tomorrow}", headers=admin).json()
    assert sorted(r["report_id"] for r in unacked) == [ids[6], ids[9], ids[10]]


def test_用得上部分索引_SQLite(client, admin, world):
    """把各接口实际发出的、按 `exam_reports.critical` 筛的语句逐条拿去 EXPLAIN QUERY PLAN（同一份参数）。"""
    seen: list[tuple[str, str, object]] = []
    current = [""]

    def _record(conn, cursor, statement, parameters, context, executemany):
        if re.search(r"exam_reports\.critical (=|IS)", statement):
            seen.append((current[0], statement, parameters))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        for label, path, headers in _calls(admin, world["doctor"]):
            current[0] = label
            assert client.get(path, headers=headers).status_code == 200, path
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    # 覆盖面自证：每条路都真发出了按危急值筛的语句（清单加计数的各两条，这里只数路）
    assert sorted({label for label, _, _ in seen}) == sorted(label for label, _, _ in _calls(admin, world["doctor"]))
    with engine.connect() as conn:
        for label, statement, parameters in seen:
            plan = " | ".join(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))
            assert INDEX.search(plan), f"{label} 没用上危急值部分索引：{plan}\n{statement}"


def test_判据一处也不许写回_is_():
    """`ExamReport.critical.is_(True)` 编译成 `IS 1` / `IS true`，用不上部分索引，而且不报错、不变红——只能静态拦。"""
    hits = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("is_", "is_not", "isnot")
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "critical"
                and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == "ExamReport"
            ):
                hits.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert hits == [], f"危急值判据写回了 `.is_(…)`，用不上部分索引，改成 `ExamReport.critical == true()`：{hits}"


@pytest.mark.integration
@pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL")
def test_用得上部分索引_PG():
    """库由 conftest 的 `_pg_test_db_at_heads` 推到 heads；只读，不写任何行（共用库）。

    关掉顺序扫描后，只取一个没有索引的列：能服务这条查询的只剩部分索引本身——谓词证得出就用它，证不出就退回被关掉的
    顺序扫描。PG 16 上 `critical IS true` 属后者（2026-09-30 单用户模式实测），这里只断言现行写法属前者。"""
    pg = create_engine(PG_URL)
    try:
        with pg.connect() as conn:   # 全程一个事务，出 with 时回滚：SET LOCAL 随之失效
            definition = conn.execute(text("SELECT pg_get_indexdef('ix_exam_reports_critical'::regclass)")).scalar()
            assert definition.endswith("USING btree (critical) WHERE critical"), definition
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            stmt = select(ExamReport.conclusion).where(ExamReport.critical == true())
            sql = str(stmt.compile(dialect=pg.dialect, compile_kwargs={"literal_binds": True}))
            plan = " | ".join(row[0] for row in conn.exec_driver_sql("EXPLAIN " + sql))
            assert INDEX.search(plan), f"没用上危急值部分索引：{plan}"
    finally:
        pg.dispose()
