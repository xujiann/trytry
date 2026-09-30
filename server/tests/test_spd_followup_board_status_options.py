"""随访看板的状态下拉缺「已移除」：被移除的随访在页面上找不回来（P2-1024，第二十九批「前后端取值表」扫描 E1-6）。

后端 `FOLLOWUP_STATUS_NAMES` 五种状态（待随访 / 已完成 / 已超期 / 已移除 / 失访），看板的状态下拉手写了三种；调整弹窗能把随访
「移除」、也能「恢复为待随访」，可移除的那条在页面上四种查法（全部 / 三种状态 / 只看超期）都查不到——清单 limit 30 不翻页，
已完成的一多，「全部」里也翻不到它。同页复诊看板的下拉取自 `SPD_REVISIT_STATUS`，含「已移除」。

修法：下拉补「已移除」，措辞照后端；「已超期」不进下拉，走「只看超期」（那条会先跑一次超期扫描，P2-828 已定）。
"""
import re
from pathlib import Path

from app.spd.routers.followup import FOLLOWUP_STATUS_NAMES

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def _board_status_options():
    start = PAGE.index('<form class="inline" id="spd-fu-filter">')
    select = PAGE[start:PAGE.index("</select>", start)]
    return dict(re.findall(r'<option value="(\w+)">([^<]+)</option>', select))


def test_状态下拉与后端同码同名_超期走勾选():
    options = _board_status_options()
    assert options == {k: v for k, v in FOLLOWUP_STATUS_NAMES.items() if k != "overdue"}   # 修前缺 removed
    start = PAGE.index('<form class="inline" id="spd-fu-filter">')
    assert '<input type="checkbox" name="overdue" value="true"> 只看超期' in PAGE[start:PAGE.index("</form>", start)]


def test_按已移除查得到被移除的随访(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21024 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21024 随访", "id_card": "110101196602021024", "birth_date": "1966-02-02"}).json()["id"]
    assert client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).status_code == 201
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P21024_R0", "name": "P21024 当日随访", "scene": "outpatient", "points": [0]}).json()["id"]
    plan = client.post("/api/spd/followup-plans", headers=admin, json={"patient_id": patient, "rule_id": rule, "org_id": org})
    assert plan.status_code == 201, plan.text
    record = plan.json()["items"][0]["id"]
    removed = client.patch(f"/api/spd/followup-records/{record}", headers=admin, json={"status": "removed"})
    assert removed.status_code == 200, removed.text
    rows = client.get("/api/spd/followup-records", headers=admin, params={"status": "removed", "patient_id": patient}).json()
    assert [(r["id"], r["status_name"]) for r in rows] == [(record, "已移除")]   # 下拉选「已移除」送的就是这一句
