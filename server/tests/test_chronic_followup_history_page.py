"""慢病随访只能录、不能看：上次量的血压多少、给过什么指导，页面上查不到（P2-478，第八批扫描 V2-5）。

慢病管理页的「随访录入」提交后只弹一次分级回执；`GET /api/chronic/{chronic_id}/followups` 一直在（按患者可见性判、
留痕），前端一个调用都没有。清单出参（`FollowUpOut`）也不带随访时刻——就算有页面调，也看不出是哪天量的。

修法：在管名单每行一个「随访记录」，按次列出随访时间、血压、血糖、其他指标（按病种目录的指标名显示）、指导与
下次随访；清单出参单开 `FollowUpHistoryOut`，多带 `created_at`——新建回执里的 `followup` 键序由契约用例钉着，不动。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_chronic() -> str:
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    start = source.index("async function renderChronic()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_在管名单每行能看随访记录():
    body = _render_chronic()
    assert 'data-fuhist="${c.id}">随访记录</button>' in body   # 修前只有「风险评分」
    assert "api(`/api/chronic/${fuhist}/followups`)" in body
    table = body[body.index('table(["随访时间", "血压", "血糖", "其他指标", "指导", "下次随访"]'):]
    table = table[:table.index("</tr>`)")]
    for field in ("f.created_at", "f.sbp", "f.dbp", "f.glucose", "f.metrics", "f.guidance", "f.next_due"):
        assert field in table, field
    assert "esc(metricNames[k] || k)" in table   # 指标按病种目录里的名字显示


def test_清单带随访时刻_新的在前(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2478 患者", "id_card": "330106196505052478"}).json()["id"]
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2478 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    cid = chronic.json()["id"]
    for sbp in (168, 142):
        created = client.post(f"/api/chronic/{cid}/followups", headers=admin, json={"sbp": sbp, "dbp": 90})
        assert created.status_code == 201, created.text
        assert "created_at" not in created.json()["followup"]   # 回执的键序不动（test_chronic_contract 钉着）
    rows = client.get(f"/api/chronic/{cid}/followups", headers=admin)
    assert rows.status_code == 200, rows.text
    assert [r["sbp"] for r in rows.json()] == [142, 168]
    for r in rows.json():
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", r["created_at"]), r   # 修前没有这个键
