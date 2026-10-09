"""审方规则的改动记录（P2-578，第十一批「落库快照 vs 现查配置」扫描 Y3-1 的后一半）。

审方页与停用接口都写着「规则改过什么、什么时候不再生效，处方点评复核时要回溯得到」；可规则导入按 drug_code 整条覆盖，
覆盖之后旧值就没了——`drug_rules` 没有更新时间，审计日志只记方法、路径与状态码。修后新建、导入（新建或覆盖）、停用、
恢复各记一条改动前后，规则表每行有「改动记录」按钮。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import DrugRuleChange

CODE = "P2578-AMOX"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def history(client, admin):
    """新建 → 导入覆盖（改上限与单位）→ 导入同样的值（不算改过）→ 停用 → 恢复。"""
    created = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": CODE, "max_daily_dose": 3, "dose_unit": "g", "antibiotic": True, "ddd": 1.5})
    assert created.status_code == 201, created.text
    # 第二次导入同样的值：回执记「未改」、不算覆盖更新，与改动记录不记它同一个判法（P2-1665，修前两次都报 updated 1）
    for dose, unit, updated in ((3000, "mg", 1), (3000, "mg", 0)):
        imported = client.post("/api/prescriptions/rules/import", headers=admin, json=[{
            "drug_code": CODE, "max_daily_dose": dose, "dose_unit": unit, "antibiotic": True, "ddd": 1.5}])
        assert imported.status_code == 200, imported.text
        assert (imported.json()["updated"], imported.json()["unchanged"]) == (updated, 1 - updated), imported.text
    assert client.delete(f"/api/prescriptions/rules/{CODE}", headers=admin).status_code == 200
    assert client.post(f"/api/prescriptions/rules/{CODE}/reactivate", headers=admin).status_code == 200
    resp = client.get(f"/api/prescriptions/rules/{CODE}/changes", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_每次改动一条_最新在前_同样的值再导入不算改过(history):
    assert [c["action"] for c in history] == ["reactivate", "deactivate", "import", "create"]
    assert [c["action_name"] for c in history] == ["恢复", "停用", "导入", "新建"]
    assert all(c["changed_by"] == "平台管理员" and c["at"][:4].isdigit() for c in history)


def test_导入覆盖记下改前改后_只列改了的项(history):
    imported = history[2]
    assert {(x["label"], x["before"], x["after"]) for x in imported["changes"]} == {
        ("日剂量上限", "3.0", "3000.0"), ("剂量单位", "g", "mg")}   # 修前覆盖之后旧值无处可查


def test_停用与恢复记生效标记(history):
    assert [(x["label"], x["before"], x["after"]) for x in history[1]["changes"]] == [("生效", "是", "否")]
    assert [(x["label"], x["before"], x["after"]) for x in history[0]["changes"]] == [("生效", "否", "是")]


def test_新建列出全部字段(history):
    created = {x["label"]: x for x in history[3]["changes"]}
    assert created["日剂量上限"]["before"] == "—" and created["日剂量上限"]["after"] == "3.0"
    assert created["抗菌药物"]["after"] == "是" and created["生效"]["after"] == "是"


def test_导入新建也记_撞了唯一约束的新建不留记录(client, admin):
    fresh = client.post("/api/prescriptions/rules/import", headers=admin, json=[{
        "drug_code": "P2578-NEW", "max_daily_dose": 1}])
    assert fresh.json() == {"imported": 1, "updated": 0, "unchanged": 0}
    rows = client.get("/api/prescriptions/rules/P2578-NEW/changes", headers=admin).json()
    assert [c["action"] for c in rows] == ["import"]
    dup = client.post("/api/prescriptions/rules", headers=admin, json={"drug_code": "P2578-NEW", "max_daily_dose": 2})
    assert dup.status_code == 409
    with SessionLocal() as db:
        assert db.query(DrugRuleChange).filter(DrugRuleChange.drug_code == "P2578-NEW").count() == 1


def test_没有这条规则的404(client, admin):
    assert client.get("/api/prescriptions/rules/P2578-NONE/changes", headers=admin).status_code == 404


def test_规则表每行有改动记录入口():
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    assert 'data-rulelog="${esc(r.drug_code)}">改动记录</button>' in source
    handler = source[source.index("if (rulelog) {"):]
    handler = handler[:handler.index("return;")]
    assert "api(`/api/prescriptions/rules/${encodeURIComponent(rulelog)}/changes`)" in handler
    for piece in ("c.action_name", "c.changed_by", "x.label", "x.before", "x.after"):   # 文案取自后端
        assert piece in handler, piece
