"""慢病随访录入（管理端与医生移动端）补「本次指导」（P2-862，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-11）。

`FollowUpCreate.guidance` 早就收，随访史清单也有「指导」一列（P2-478 只补了「看得见」）；两处录入表单都没有这一项，界面录的
随访「指导」恒为空。修后两处都补上。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_管理端随访录入送本次指导():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    start = core.index('<form class="inline" id="fu-form">')
    assert '<input name="guidance"' in core[start:core.index("</form>", start)]   # 修前没有
    assert 'guidance: String(f.get("guidance") || "").trim() }' in core


def test_移动端随访录入送本次指导():
    html = (STATIC / "m" / "doctor.html").read_text(encoding="utf-8")
    start = html.index('<form id="fu-form"')
    assert '<textarea id="fu-guidance"' in html[start:html.index("</form>", start)]
    js = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    assert 'guidance: $("#fu-guidance").value.trim() };' in js


def test_按页面送的指导_随访史里看得到(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2862 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2862 患者", "id_card": "330102196001012862"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org}).json()["id"]
    made = client.post(f"/api/chronic/{chronic}/followups", headers=admin, json={
        "sbp": 150, "dbp": 92, "metrics": {}, "next_due": "", "guidance": "限盐，每日监测血压"})
    assert made.status_code in (200, 201), made.text
    rows = client.get(f"/api/chronic/{chronic}/followups", headers=admin).json()
    assert [r["guidance"] for r in rows] == ["限盐，每日监测血压"]
