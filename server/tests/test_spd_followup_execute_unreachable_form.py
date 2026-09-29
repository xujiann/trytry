"""执行随访弹窗补「联系结果」，打不通能记成失访（P2-854，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-3）。

`execute_followup` 的 docstring：失访（`unreachable`）单独一个状态而不是「完成但没答案」——随访完成率的分母含失访、分子
不含，两者混在一起会把完成率算高。看板筛选里也有「失访」。页面的执行弹窗却只有渠道、问卷与结果，不送 `unreachable`；
「调整」弹窗只有移除 / 恢复——打不通只能记成「已完成」。修后弹窗加「联系结果」，选「未联系上」送 `unreachable: true`。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_执行弹窗有联系结果_选未联系上送失访():
    start = PAGE.index("if (exec) {")
    body = PAGE[start:PAGE.index("if (call) {", start)]
    assert '{ name: "outcome", label: "联系结果", type: "select", value: "done",' in body   # 修前没有这一项
    assert '{ value: "unreachable", label: "未联系上（记失访，不计完成）" }' in body
    assert 'unreachable: form.outcome === "unreachable"' in body


def test_按页面送的失访_状态是失访不计完成(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2854 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2854 患者", "id_card": "330102196001012854"}).json()["id"]
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P2854_FR", "name": "P2854 随访", "points": [0]})
    assert rule.status_code == 201, rule.text
    plan = client.post("/api/spd/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "org_id": org})
    assert plan.status_code in (200, 201), plan.text
    record = plan.json()["items"][0]["id"]
    done = client.post(f"/api/spd/followup-records/{record}/execute", headers=admin, json={
        "channel": "phone", "result": "三次未接通", "answers": {}, "unreachable": True})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "unreachable"
