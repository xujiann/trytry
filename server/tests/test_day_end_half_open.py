"""「截至某日（含）」按 `<= 日期 23:59:59` 筛时间戳，最后一秒里带毫秒的记录被截在外面（P2-335）。

`DateTime` 列带微秒（`utcnow()`）：23:59:59.5 入库的记录大于「23:59:59」。原先 12 处这么写——考核取数 8 处（任务、转诊、
个案上报、纳管、评估、路径、监测……）、敏感读留痕稽核、呼叫任务、个案上报、转诊清单——那一秒里的记录在前后两段都不算。

修法：`deps.through_day(column, day)`，左闭右开取到次日 00:00:00（与 `deps.period_bounds` 同一个口径）。
本文件：判据闸门（比较式里不许再出现 `23:59:59`）+ 助手的边界 + 三个取数口径各一条端点回归。
"""
import ast
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Column, DateTime, MetaData, Table

from app.database import SessionLocal

APP = Path(__file__).resolve().parents[1] / "app"
LAST_SECOND = datetime(2026, 8, 31, 23, 59, 59, 500000)


# ================================================================ 判据闸门
def _day_end_compares() -> list[str]:
    """比较式里带 `23:59:59` 字面量的位置（f-string 或普通字符串）——「截至某日」的闭区间写法。"""
    hits = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare) and "23:59:59" in ast.unparse(node):
                hits.append(f"{path.relative_to(APP).as_posix()}:{node.lineno}")
    return hits


def test_不再用23点59分59秒当日界():
    hits = _day_end_compares()
    assert hits == [], (
        "「截至某日（含）」请用 `deps.through_day(column, day)`（左闭右开到次日 00:00:00）——"
        "`<= f\"{day} 23:59:59\"` 截掉最后一秒里带毫秒的记录：\n  " + "\n  ".join(hits)
    )


def test_判据自证(tmp_path, monkeypatch):
    probe = tmp_path / "probe.py"
    probe.write_text(
        'q = q.filter(T.created_at <= f"{end} 23:59:59")\n'   # 该命中
        'x = "23:59:59"\n'                                    # 不在比较式里，不算
        'q = q.filter(T.created_at >= f"{start} 00:00:00")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "APP", tmp_path)
    assert _day_end_compares() == ["probe.py:1"]


# ================================================================ 助手的边界
def _col():
    return Table("probe_t", MetaData(), Column("at", DateTime)).c.at


def test_次日零点为开区间右端_年末跨年():
    from app.deps import through_day

    cond = through_day(_col(), "2026-12-31")
    assert str(cond.compile(compile_kwargs={"literal_binds": True})) == "probe_t.at < '2027-01-01 00:00:00'"


def test_9999年末没有次日_不设上界():
    from app.deps import through_day

    assert str(through_day(_col(), "9999-12-31").compile()) == "true"


# ================================================================ 端点回归：最后一秒的记录算进当天
@pytest.fixture(scope="module")
def patient(client, admin):
    got = client.post("/api/patients", headers=admin, json={"name": "P2335 日界患者", "id_card": "330106197002282335"})
    assert got.status_code in (200, 201), got.text
    return got.json()["id"]


def test_敏感读留痕稽核_截至当日含最后一秒(client, admin, patient):
    from app.models import AccessLog

    with SessionLocal() as db:
        db.add(AccessLog(username="p2335-auditor", patient_id=patient, resource="archive", basis="global",
                         created_at=LAST_SECOND))
        db.commit()
    got = client.get("/api/access-logs", headers=admin,
                     params={"username": "p2335-auditor", "start": "2026-08-31", "end": "2026-08-31"})
    assert got.status_code == 200, got.text
    assert len(got.json()) == 1   # 修前 0


def test_呼叫任务清单_截至当日含最后一秒(client, admin, patient):
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        db.add(SpdCallTask(patient_id=patient, status="connected", created_at=LAST_SECOND))
        db.commit()
    got = client.get("/api/spd/call-tasks", headers=admin, params={
        "patient_name": "P2335 日界患者", "date_from": "2026-08-31", "date_to": "2026-08-31"})
    assert got.status_code == 200, got.text
    assert len(got.json()) == 1   # 修前 0


def test_考核取数_期末最后一秒的任务算进本期(client, admin, patient):
    from app.spd.models import SpdIndicator, SpdTask
    from app.spd.routers.assess import collect_metrics

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2335 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        db.add(SpdTask(patient_id=patient, title="P2335 期末任务", task_type="followup", org_id=org, status="done",
                       created_at=LAST_SECOND))
        db.commit()
        metrics = collect_metrics(db, SpdIndicator(code="P2335", name="P2335", data_source="task"), "org", org, "2026-08")
    assert (metrics["total"], metrics["done"]) == (1.0, 1.0)   # 修前 (0, 0)
