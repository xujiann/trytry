"""慢专病「成员服务」页的量表评估只有统计、看不到记录（P2-563）。

需求对照表中心端 #8 写「查看评估对象、记录与统计结果」，实现栏列了 `GET /api/spd/assessments`；接口一直在（按可见患者收口、
可按患者 / 量表 / 风险等级 / 病种筛），前端一个调用都没有（读动词棘轮登记在册）。页面上画了 `#spd-assess-list` 容器却从不填：
评了谁、哪次评的、得几分，一条也看不到。

修法：量表评估一块补「查评估记录」：四个筛选条件都可空（接口本身按可见患者收口），列出时间、患者、量表与版本、病种、得分、
风险等级、建议，写明条数，截到 50 条时明说。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TABLE_KEYS = ("created_at", "patient_name", "patient_id", "scale_code", "scale_version", "program_code", "score",
              "risk_level", "advice")


def _render_member() -> str:
    source = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    start = source.index("async function renderSpdMember()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_评估记录有查询入口_四个筛选条件():
    body = _render_member()
    form = body[body.index('<form class="inline" id="spd-assess-query"'):]
    form = form[:form.index("</form>")]
    for name in ("patient_id", "scale_code", "risk_level", "program_code"):
        assert f'name="{name}"' in form, name
    handler = body[body.index('$("#spd-assess-query").onsubmit'):]
    handler = handler[:handler.index('$("#spd-intvtpl-form")')]
    assert "api(`/api/spd/assessments?${params}`)" in handler   # 修前没有一个调用
    assert '$("#spd-assess-list").innerHTML' in handler           # 修前容器画了从不填
    assert "已截到 ${SPD_ASSESS_LIMIT} 条" in handler
    for key in TABLE_KEYS:
        assert f"a.{key}" in handler, key


def test_列表返回表格要读的键_按风险等级筛(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2563 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2563 患者", "id_card": "330127196001012563"}).json()["id"]
    assert client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).status_code == 201
    answers = {k: "是" for k in ("family", "salt", "overweight", "smoke", "drink", "symptom")}
    created = client.post("/api/spd/assessments", headers=admin, json={
        "patient_id": patient, "scale_code": "scr_hypertension", "answers": answers, "program_code": "hypertension"})
    assert created.status_code == 201, created.text
    rows = client.get("/api/spd/assessments", headers=admin, params={
        "patient_id": patient, "risk_level": "high", "limit": 50}).json()
    assert [r["id"] for r in rows] == [created.json()["id"]]
    assert set(TABLE_KEYS) <= set(rows[0])
    assert rows[0]["patient_name"] == "P2563 患者"
    assert client.get("/api/spd/assessments", headers=admin, params={
        "patient_id": patient, "risk_level": "low"}).json() == []
