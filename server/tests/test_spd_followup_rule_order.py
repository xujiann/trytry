"""随访方案匹配取「命中的第一套」，可候选方案的查询不排序：两套都命中时派生哪套由库的返回次序决定（P2-369）。

出院即派生（`subscribers.on_admission_discharged`）与按患者特征自动匹配（`auto_match_plans`）都是
`next(r for r in query.all() if 关键词命中)`，没有 ORDER BY。PG 不保证无 ORDER BY 的次序（改过的行排到堆尾，P2-304 在真 PG
上实测过）：改一下方案名称，同一类出院患者就从甲方案换成乙方案。

修法：两处都按方案编号排，与规则试算（P2-304）同一个次序。SQLite 无 ORDER BY 时按 rowid 返回、恰好就是编号序，行为上
测不出修前修后之分——防回退靠静态钉；要不要「命中几套就派生几套」另行登记待裁定。
"""
import ast
import inspect
import textwrap

B = "/api/spd"


def _rule_queries(func):
    """函数里所有 `db.query(SpdFollowupRule)…` 链的源码。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    chains = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all":
            text = ast.unparse(node)
            if "query(SpdFollowupRule)" in text:
                chains.append(text)
    return chains


def test_出院即派生按方案编号取第一套():
    from app.spd import subscribers

    chains = _rule_queries(subscribers.on_admission_discharged)
    assert chains and all("order_by(SpdFollowupRule.id)" in c for c in chains), chains   # 修前不排序


def test_自动匹配按方案编号取第一套():
    from app.spd.routers import followup

    chains = _rule_queries(followup.auto_match_plans)
    assert chains and all("order_by(SpdFollowupRule.id)" in c for c in chains), chains   # 修前不排序


def test_两套方案都命中_自动匹配派生编号小的那套(client, admin):
    """行为上记下约定的次序（SQLite 上修前修后都绿，见模块文档）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2369 随访卫生院", "org_type": "township", "level": "township"}).json()["id"]
    rule_ids = []
    for code in ("P2369_A", "P2369_B"):
        created = client.post(f"{B}/followup-rules", headers=admin, json={
            "code": code, "name": f"{code} 方案", "scene": "outpatient", "diagnosis_keywords": ["P2369病"],
            "points": [7]})
        assert created.status_code == 201, created.text
        rule_ids.append(created.json()["id"])
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2369 患者", "id_card": "330127196606062369"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "encounter_type": "outpatient", "diagnosis_name": "P2369病"})
    assert encounter.status_code == 201, encounter.text
    got = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "outpatient", "org_id": org, "days": 7})
    assert got.status_code == 200 and got.json()["created"] == 1, got.text
    with SessionLocal() as db:
        used = {r for (r,) in db.query(SpdFollowupRecord.rule_id).filter(SpdFollowupRecord.patient_id == patient)}
    assert used == {min(rule_ids)}
