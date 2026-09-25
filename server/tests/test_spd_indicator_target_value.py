"""考核指标在页面上改了目标值，计分照旧按原来的目标值：「目标值」一栏改了等于没改（P2-104）。

按比例计分（`score_rule.type == "ratio"`）的目标，`score_of` 原先先取评分规则里的 `target`，规则里没写才取指标的
`target_value`。种子里的 12 个指标两处都写了同一个数；管理端「考核指标库」的编辑弹窗只改得到 `target_value`（页面
描述写着「各县只需调权重与目标值」），评分规则界面上改不到——于是县里把纳管率的目标从 80 调成 60，指标库里显示 60，
跑分、扣分理由（「未达目标值80.0」）照旧按 80。

修法：目标值以指标的 `target_value` 为准（界面上看到、改到的就是它），指标没设目标值才看评分规则里的。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2104 考核院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2104 患者", "id_card": "330127196802022104"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    return {"org": org}


def _run(client, admin, org, code, target_value, rule):
    ind = client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": "enrollment", "object_type": "org",
        "formula": "enrolled", "target_value": target_value, "score_rule": rule})
    assert ind.status_code == 201, ind.text
    return ind.json()["id"]


def _detail(client, admin, org, code):
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": f"{code}_PLAN", "name": f"{code} 考核", "level": "township", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": code, "weight": 100}]})
    assert plan.status_code == 201, plan.text
    # 远期的考核期：纳管口径只看「期末之前建的档」，与今天是哪天无关
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan.json()["id"], "period": "2099-12", "object_ids": [org]})
    assert run.status_code == 200, run.text[:300]
    score_id = client.get(f"{B}/scores?plan_id={plan.json()['id']}", headers=admin).json()[0]["id"]
    return client.get(f"{B}/scores/{score_id}", headers=admin).json()["detail"][0]


def test_页面上改了目标值_计分按改后的(client, admin, world):
    """与种子同一个写法：规则里、指标上各写一份 80；页面的编辑弹窗只改得到指标上的那份。"""
    ind = _run(client, admin, world["org"], "P2104_A", 80, {"type": "ratio", "full": 100, "target": 80})
    edited = client.patch(f"{B}/indicators/{ind}", headers=admin, json={"target_value": 50})
    assert edited.status_code == 200 and edited.json()["target_value"] == 50, edited.text
    detail = _detail(client, admin, world["org"], "P2104_A")
    assert detail["reason"] == "未达目标值50.0（实际1.0）", detail   # 修前：未达目标值80.0
    assert detail["score"] == 2.0, detail                            # 修前：1.25（1 / 80 × 100）


def test_指标没设目标值_照旧看评分规则里的(client, admin, world):
    _run(client, admin, world["org"], "P2104_B", None, {"type": "ratio", "full": 100, "target": 4})
    detail = _detail(client, admin, world["org"], "P2104_B")
    assert detail["reason"] == "未达目标值4.0（实际1.0）", detail
