"""公卫事件的处置记录只记得进、页面上看不到：哪起事件做过什么、谁做的，结案之后更是无从查起（P2-477，第八批扫描 V2-5）。

「公卫协同」页的事件列表只给处置中的事件两个按钮：「处置记录」（其实是登记一条）与「结案」；
`GET /api/publichealth/events/{event_id}/actions` 一直在，前端一个调用都没有——记了之后页面上不见踪影，
已结案的事件连按钮都没有（操作栏一个「—」）。

修法：每起事件（含已结案的）一个「查看处置」，在事件列表下方列出处置动作、执行人与时间；看哪一起只留在内存里；
登记按钮改叫「登记处置」，记完展开这一起，刚记的那条就在眼前。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_public_health() -> str:
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderPublicHealth()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_每起事件都能看处置记录_结案的也能():
    body = _render_public_health()
    assert "api(`/api/publichealth/events/${viewing.id}/actions`)" in body   # 修前没有一个调用
    row = body[body.index('table(["ID", "事件", "级别", "病种", "状态", "操作"]'):]
    row = row[:row.index("</tr>`)")]
    # 「查看处置」在「处置中」的条件之外：已结案的也要能看
    assert '<td><button class="btn secondary" data-view="${ev.id}">查看处置</button>${ev.status === "active" ? `' in row
    records = body[body.index('table(["时间", "处置动作", "执行人"]'):]
    records = records[:records.index("</tr>`)")]
    for field in ("a.at", "a.action", "a.actor"):
        assert field in records, field


def test_看哪一起只留在内存里_记完展开这一起():
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    assert "const PH_EVENT_VIEW = { id: 0 };" in source
    body = _render_public_health()
    assert "PH_EVENT_VIEW.id = Number(view);" in body
    handler = body[body.index("if (act) {"):]
    handler = handler[:handler.index("if (close) {")]
    assert handler.index("PH_EVENT_VIEW.id = Number(act);") < handler.index("postAction(")
    assert 'data-act="${ev.id}">登记处置</button>' in body   # 修前叫「处置记录」，其实是登记


def test_结案之后处置记录照样查得到(client, admin):
    event = client.post("/api/publichealth/events", headers=admin, json={
        "title": "P2477 聚集性腹泻", "level": "IV", "disease_name": "诺如病毒"}).json()
    for action, actor in (("流行病学调查", "疾控甲"), ("环境消杀", "疾控乙")):
        created = client.post(f"/api/publichealth/events/{event['id']}/actions", headers=admin,
                              json={"action": action, "actor": actor})
        assert created.status_code == 201, created.text
    assert client.post(f"/api/publichealth/events/{event['id']}/close", headers=admin).status_code == 200
    rows = client.get(f"/api/publichealth/events/{event['id']}/actions", headers=admin)
    assert rows.status_code == 200, rows.text
    assert [(r["action"], r["actor"]) for r in rows.json()] == [("流行病学调查", "疾控甲"), ("环境消杀", "疾控乙")]
    assert all(r["at"] for r in rows.json())
