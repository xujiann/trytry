"""合同到期提醒不数已续签的旧合同、不数离职的人的合同；到期了还没续签的照旧提醒（P2-207）。

提醒只看「履行中 + 止期在窗口内」，而合同状态从来没有代码去改（不写 expired / terminated）：续签了的旧合同、离职的人
的合同永远挂在提醒里，每到期一份就多一条，人事页「60 天内到期合同」只增不减，每日扫描天天告警。
接口与每日扫描共用一个口径（`contract_expiring_condition`）。
"""
from datetime import date

import pytest

from app.database import SessionLocal

TODAY = "2026-09-26"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2207 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    emps = {}
    for key in ("renewed", "due", "overdue", "left"):
        emps[key] = client.post("/api/mgmt/employees", headers=admin, json={
            "org_id": org, "name": f"P2207 {key}", "position": "护士"}).json()["id"]
    contracts = [("renewed", "P2207-OLD", "2025-01-01", "2025-12-31"), ("renewed", "P2207-NEW", "2026-01-01", "2028-12-31"),
                 ("due", "P2207-DUE", "2024-10-16", "2026-10-15"), ("overdue", "P2207-LATE", "2023-06-01", "2026-05-31"),
                 ("left", "P2207-GONE", "2024-04-01", "2026-03-31")]
    for key, no, start, end in contracts:
        resp = client.post("/api/mgmt/staff-contracts", headers=admin, json={
            "employee_id": emps[key], "contract_no": no, "start_date": start, "end_date": end})
        assert resp.status_code == 201, resp.text
    assert client.post(f"/api/mgmt/employees/{emps['left']}/changes", headers=admin,
                       json={"change_type": "leave"}).status_code == 201
    return emps


def test_到期提醒只列没续签且人还在的(client, admin, world):
    rows = client.get(f"/api/mgmt/staff-contracts/expiring?days=60&today={TODAY}", headers=admin).json()
    assert [r["contract_no"] for r in rows] == ["P2207-LATE", "P2207-DUE"]   # 修前还有 OLD 与 GONE


def test_每日扫描与接口同一个数(client, world, monkeypatch):
    from app import clock
    from app.jobs import contract_expiry_scan

    monkeypatch.setattr(clock, "today", lambda: date.fromisoformat(TODAY))
    with SessionLocal() as db:
        count, _ = contract_expiry_scan(db)
    assert count == 2                                                         # 修前 4


def test_摘要与人事页照实写含已到期未续签(client, world, monkeypatch):
    """提醒里特意留着已到期还没续签的（上面的 P2207-LATE），摘要、卡片、面板却都写「60 天内到期」（P2-716，与制剂
    P2-694 同一句式）：人事看着「60 天内到期 2 份」去翻 60 天内的，已经断签四个月的那份被当成数错了。"""
    import pathlib

    from app import clock
    from app.jobs import contract_expiry_scan

    monkeypatch.setattr(clock, "today", lambda: date.fromisoformat(TODAY))
    with SessionLocal() as db:
        count, summary = contract_expiry_scan(db)
    assert summary == f"60 天内到期或已到期未续签合同 {count} 份"   # 修前「60 天内到期合同 2 份」
    page = (pathlib.Path(__file__).resolve().parents[1] / "app/static/pages-clinical.js").read_text(encoding="utf-8")
    assert "60天内到期/已到期未续签合同</div>" in page and "60天内到期合同" not in page
    assert "合同到期提醒（60天内到期或已到期未续签 ${expiringContracts.length} 份" in page
