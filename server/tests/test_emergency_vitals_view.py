"""急救途中体征只能录、不能看；录入弹窗只有心率（P2-491，梳理读动词欠账时发现）。

`POST /api/emergency/cases/{id}/vitals` 的注释写着「车载终端回传生命体征——院内可实时调阅，实现院前院内无缝对接」，
`GET` 同一路径一直在，前端一个调用都没有（读动词棘轮 P2-475 登记在册）；急救页「回传体征」的弹窗只有心率与备注，
接口收的收缩压、舒张压、血氧录不进去；出参也不带回传时刻。

修法：每起事件（含已收治的）一个「途中体征」，列出回传时刻、心率、血压、血氧、备注；回传弹窗按入参模型补齐各项
（仍是文本框自己解析：0 记 0、留空记未测，P2-249）；出参补 `created_at`。
"""
import re
from pathlib import Path

from app.routers.emergency import VitalCreate

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_emergency() -> str:
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderEmergency()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_每起事件都能看途中体征_收治了也能():
    body = _render_emergency()
    assert "api(`/api/emergency/cases/${caseId}/vitals`)" in body   # 修前没有一个调用
    acts = body[body.index("const acts = ["):body.index("].filter(Boolean);")]
    view = acts.index('data-vitals="${c.id}">途中体征</button>')
    admitted_only = acts.index('c.status !== "admitted" ?')
    record_branch_end = acts.index('` : "",', admitted_only)
    assert not admitted_only < view < record_branch_end, "「途中体征」不能挂在「未收治」的条件里"
    table = body[body.index('table(["回传时刻", "心率", "血压", "血氧 %", "备注"]'):]
    table = table[:table.index("</tr>`)")]
    for field in ("v.created_at", "v.heart_rate", "v.sbp", "v.dbp", "v.spo2", "v.note"):
        assert field in table, field


def test_回传弹窗覆盖入参模型的每一项测量值():
    body = _render_emergency()
    modal = body[body.index('spdModal("回传生命体征", ['):]
    modal = modal[:modal.index("]);")]
    measured = set(VitalCreate.model_fields) - {"note"}
    missing = measured - set(re.findall(r'name: "(\w+)"', modal))
    assert not missing, f"回传弹窗缺这几项（修前只有心率）：{sorted(missing)}"


def test_出参带回传时刻(client, admin):
    case = client.post("/api/emergency/cases", headers=admin, json={
        "location": "P2491 事发地", "symptom": "胸痛", "dest_org_id": None}).json()
    created = client.post(f"/api/emergency/cases/{case['id']}/vitals", headers=admin,
                          json={"heart_rate": 0, "sbp": 80, "dbp": 50, "spo2": 88, "note": "心跳骤停后复苏"})
    assert created.status_code == 201, created.text
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", created.json()["created_at"]), created.json()   # 修前没有这个键
    rows = client.get(f"/api/emergency/cases/{case['id']}/vitals", headers=admin).json()
    assert [(r["heart_rate"], r["sbp"], r["spo2"]) for r in rows] == [(0, 80, 88)]
    assert rows[0]["created_at"] == created.json()["created_at"]
