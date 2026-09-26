"""慢病风险评分的趋势修正按指标方向认「变差 / 好转」（P2-126）。

评分 = 分级基础分 + 趋势修正。修正原先一律「上升 +15、下降 −10」——写的时候趋势指标只有血压、血糖（越高越危）。
病种目录（块 1）之后，趋势指标取目录里的第一个分级指标，严重精神障碍的是「用药依从性评分」（`direction: low`，
越低越危）：依从性从 3 分升到 9 分（好转）评分 +15，从 9 分跌到 2 分（变差）反倒 −10；页面上把这个数原样当「评分」给人看。

修法：越高越危的指标上升算变差，越低越危的下降算变差；`trend` 照旧说数值的走向。越高越危的指标行为不变。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "风险趋势方向卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _chronic(client, admin, org, disease, key, values):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "风险趋势患者", "id_card": f"33010619800606{next(_CARDS):04d}", "gender": "男"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": disease, "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    chronic_id = chronic.json()["id"]
    for value in values:
        resp = client.post(f"/api/chronic/{chronic_id}/followups", headers=admin, json={"metrics": {key: value}})
        assert resp.status_code == 201, resp.text
    return client.get(f"/api/chronic/{chronic_id}/risk", headers=admin).json()


def test_越低越危的指标下降是变差_加分(client, admin, org):
    risk = _chronic(client, admin, org, "severe_mental", "adherence_score", [8, 5])   # 依从性 8 → 5：变差，定级 2 级
    assert (risk["metric"], risk["level"], risk["trend"]) == ("adherence_score", 2, "falling")
    assert risk["score"] == 65   # 50 + 15；修前 40（按「下降 −10」）


def test_越低越危的指标上升是好转_减分(client, admin, org):
    risk = _chronic(client, admin, org, "severe_mental", "adherence_score", [4, 6])   # 依从性 4 → 6：好转，仍是 2 级
    assert (risk["level"], risk["trend"]) == (2, "rising")
    assert risk["score"] == 40   # 50 − 10；修前 65（按「上升 +15」）


def test_越高越危的指标不变(client, admin, org):
    rising = _chronic(client, admin, org, "stroke", "mrs_score", [2, 3])   # mRS 2 → 3：变差，2 级
    assert (rising["level"], rising["trend"], rising["score"]) == (2, "rising", 65)
    falling = _chronic(client, admin, org, "stroke", "mrs_score", [3, 2])  # 好转，仍 2 级
    assert (falling["level"], falling["trend"], falling["score"]) == (2, "falling", 40)
