"""慢病病种目录的分级规则写坏了照样存得进去，这个病种之后每一次记随访都 500（P1-125）。

分级规则（`chronic_disease_types.level_rules`）在管理端是一个 JSON 文本框：记随访时按它给患者定级（1 控制良好 /
2 需干预 / 3 高危需转诊评估）。建 / 改病种原先照单全收——阈值写成 "160"、`metrics` 写成对象、指标写成一句话，
存得进去，之后这个病种的随访一条都记不进（500）；高血压这类预置病种一样改得坏。方向写成表外的值、`require_all`
写成字符串，则是悄悄定错级。

修法：建 / 改病种查分级规则（422，其余键照旧原样透传）；修前存进去的坏规则，记随访时 422 说清楚、不 500，也不悄悄
跳过定级；风险评分取趋势指标时同目录缺失，回落兜底表。
"""
import pytest

C = "/api/chronic"
BAD = [
    ({"metrics": [{"key": "sbp", "direction": "high", "level3": "160", "level2": 140}]}, "指标 sbp 的 level3 阈值必须是数"),
    ({"metrics": {"key": "sbp", "level3": 160}}, "metrics 要写成 [{key, name, direction, level3, level2}] 这样的列表"),
    ({"metrics": ["sbp>=160"]}, "metrics 要写成 [{key, name, direction, level3, level2}] 这样的列表"),
    ({"metrics": [{"name": "收缩压", "level3": 160}]}, "每个分级指标都要有 key"),
    ({"metrics": [{"key": "sbp", "direction": "up", "level3": 160}]}, "指标 sbp 的 direction 只能是 high（越高越危）/ low（越低越危）"),
    ({"require_all": "false", "metrics": []}, "require_all 只能是 true / false"),
    ({"metrics": [{"key": "sbp", "level2": True}]}, "指标 sbp 的 level2 阈值必须是数"),
]
IDS = ["阈值是文字", "metrics是对象", "指标是一句话", "指标没有key", "方向表外", "require_all是字符串", "阈值是布尔"]


@pytest.mark.parametrize(("rules", "detail"), BAD, ids=IDS)
def test_建病种_分级规则写坏一律422(client, admin, rules, detail):
    resp = client.post(f"{C}/disease-types", headers=admin,
                       json={"code": "p125_bad", "name": "P125 坏规则", "level_rules": rules})
    assert resp.status_code == 422 and resp.json() == {"detail": f"分级规则非法：{detail}"}, resp.text[:300]


def test_改病种也查_其余键照旧透传(client, admin):
    rules = {"require_all": False, "备注": "自定义键原样透传",
             "metrics": [{"key": "ua", "name": "血尿酸", "unit": "μmol/L", "direction": "high", "level3": 540,
                          "level2": 420.5}]}
    created = client.post(f"{C}/disease-types", headers=admin,
                          json={"code": "p125_gout", "name": "P125 痛风", "level_rules": rules})
    assert created.status_code == 201 and created.json()["level_rules"] == rules, created.text
    url = f"{C}/disease-types/{created.json()['id']}"
    resp = client.patch(url, headers=admin, json={"level_rules": {"metrics": [{"key": "ua", "level3": "540"}]}})
    assert resp.status_code == 422 and resp.json() == {"detail": "分级规则非法：指标 ua 的 level3 阈值必须是数"}
    resp = client.patch(url, headers=admin, json={"guidance": "低嘌呤饮食"})   # 不动规则的改档不查
    assert resp.status_code == 200 and resp.json()["level_rules"] == rules, resp.text


def test_存量坏规则_记随访422说清楚_不500_风险评分回落兜底(client, admin):
    """修前落库的坏规则：记随访修前 500；现在 422 点明病种与问题，随访不落库（不悄悄跳过定级）。"""
    from app.database import SessionLocal
    from app.models import ChronicDiseaseType, FollowUp

    with SessionLocal() as db:
        db.add(ChronicDiseaseType(code="p125_legacy", name="P125 存量坏规则", guidance="", followup_interval_days=90,
                                  level_rules={"metrics": [{"key": "sbp", "direction": "high", "level3": "160"}]}))
        db.commit()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P125 慢病院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P125 患者", "id_card": "330106197001011904", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    chronic = client.post(C, headers=admin, json={"patient_id": patient, "disease": "p125_legacy",
                                                  "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    chronic_id = chronic.json()["id"]
    resp = client.post(f"{C}/{chronic_id}/followups", headers=admin, json={"sbp": 170, "dbp": 95})
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json() == {"detail": "病种「p125_legacy」的分级规则配置有误（指标 sbp 的 level3 阈值必须是数），"
                                     "请在病种目录里修正后再记随访"}
    with SessionLocal() as db:
        assert db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).count() == 0
    risk = client.get(f"{C}/{chronic_id}/risk", headers=admin)
    assert risk.status_code == 200, risk.text   # 修前 200（metrics 是列表时取第一个），规则写成对象时是 500


def test_预置病种的分级规则都过得了同一道校验():
    from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES
    from app.routers.chronic import level_rules_problem

    assert SEED_CHRONIC_DISEASE_TYPES
    assert [t["code"] for t in SEED_CHRONIC_DISEASE_TYPES if level_rules_problem(t["level_rules"])] == []


@pytest.mark.parametrize(("glucose", "level"), [(2.8, 3), (3.9, 3), (5.6, 1), (8.0, 2), (12.0, 3)],
                         ids=["低血糖", "恰为3.9", "正常", "偏高", "高"])
def test_糖尿病低血糖也判高危_建议转诊(client, admin, glucose, level):
    """P2-119：预置的 2 型糖尿病分级规则原先只按「越高越危」——空腹血糖 2.8 判成 1 级「控制良好」、不建议转诊，
    而国家基本公卫规范把血糖 ≤3.9 mmol/L 与 ≥16.7 并列为紧急转诊指征。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P2119 慢病院{glucose}", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2119 糖尿病{glucose}", "id_card": f"33010619700202{int(glucose * 10):04d}"}).json()["id"]
    chronic = client.post(C, headers=admin, json={"patient_id": patient, "disease": "diabetes",
                                                  "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    resp = client.post(f"{C}/{chronic.json()['id']}/followups", headers=admin, json={"glucose": glucose})
    assert resp.status_code in (200, 201), resp.text[:300]
    assert resp.json()["level"] == level   # 修前 2.8 / 3.9 判 1
    assert resp.json()["refer_up_suggested"] is (level == 3)
