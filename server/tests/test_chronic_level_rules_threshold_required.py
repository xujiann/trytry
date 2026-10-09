"""慢病分级规则里一个可用阈值都没有的指标项照存，这个病种此后永远定 1 级、不建议上转（P2-1739，第五十一批扫描 AO4-1）。

`level_rules_problem` 自己定的规矩是挡「会让随访定级抛错、或悄悄定错级的写法」（P1-125 / P2-465 / P2-886 / P2-1544 都按它
修），可一个阈值都没写的指标项原先照收：指标项不带 `level3` / `level2`，或阈值键拼成 `level_3` / `Level3`，建病种 201、
改病种 200；`_metric_level` 两档都比不上、一律回 1 级。扫描实测（修前代码，`r1_catalog.py`）：这样建的病种录收缩压 / 舒张压
220/130 都是「1 级、不建议上转」，预置高血压同值是 3 级、建议上转；`r4_manual_patch.py`：老库照运维手册补低血糖规则时把
`level3` 敲成 `level_3`，PATCH 200，空腹血糖 2.8 判 1 级、不建议上转。目录表照样印「收缩压(sbp)」，管理员看不出规则是坏的。

修法：每个指标项至少要有 `level3` / `level2` 之一，否则 422 并点名指标；挂在 `level_rules_problem` 里，建病种与改病种同一
判据，修前存进去的与 P1-125 同一口径（记随访 422 说清楚、随访不落库）。「规则非空却没有 metrics 列表」「指标项里认不出的键」
两样另定（P2-1753），不在这里。预置 8 个病种与契约用例的 GOUT_RULES 照旧通过。
"""
import pytest

from test_chronic_contract import GOUT_RULES

from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES
from app.database import SessionLocal
from app.models import ChronicDiseaseType, FollowUp
from app.routers.chronic import level_rules_problem

C = "/api/chronic"
#: 扫描原样的两种写法：指标项不带阈值；阈值键拼错（`level_3` / `Level3`）
NO_THRESHOLD = {"metrics": [{"key": "sbp", "name": "收缩压"}, {"key": "dbp", "name": "舒张压"}]}
TYPO_KEYS = {"metrics": [{"key": "sbp", "name": "收缩压", "level_3": 160, "level_2": 140},
                         {"key": "dbp", "name": "舒张压", "Level3": 100, "Level2": 90}]}


def _problem(key: str) -> str:
    return f"指标 {key} 一个阈值都没有（level3 / level2 至少写一个，键名照此拼写），这一项永远定 1 级"


def _types(client, admin) -> dict:
    return {t["code"]: t for t in client.get(f"{C}/disease-types", headers=admin).json()}


def test_判据_一个阈值都没有的指标点名():
    assert level_rules_problem(NO_THRESHOLD) == _problem("sbp")   # 修前 ""
    assert level_rules_problem(TYPO_KEYS) == _problem("sbp")
    # 前一项写对、后一项键名拼错：点名的是后一项
    assert level_rules_problem({"metrics": [{"key": "sbp", "level3": 160, "level2": 140},
                                            TYPO_KEYS["metrics"][1]]}) == _problem("dbp")
    # 两档都显式写 null 同没写
    assert level_rules_problem({"metrics": [{"key": "cat_score", "level3": None, "level2": None}]}) == _problem("cat_score")


def test_判据_只写一档的照常通过_预置与契约规则照旧():
    assert level_rules_problem({"metrics": [{"key": "glucose", "direction": "low", "level3": 3.9}]}) == ""   # 预置低血糖一条
    assert level_rules_problem({"metrics": [{"key": "mrs_score", "level2": 2}]}) == ""
    assert level_rules_problem({}) == ""   # 不写规则照旧（不评级）
    for seed in SEED_CHRONIC_DISEASE_TYPES:   # 预置的八个病种
        assert level_rules_problem(seed["level_rules"]) == "", seed["code"]
    assert level_rules_problem(GOUT_RULES) == ""


@pytest.mark.parametrize(("rules", "key"), [(NO_THRESHOLD, "sbp"), (TYPO_KEYS, "sbp")], ids=["无阈值项", "键名拼成level_3"])
def test_建病种_一个阈值都没有422(client, admin, rules, key):
    resp = client.post(f"{C}/disease-types", headers=admin,
                       json={"code": "p21739_bad", "name": "P21739 坏规则", "level_rules": rules})
    assert resp.status_code == 422, resp.text   # 修前 201，此后 220/130 定 1 级、不建议上转
    assert resp.json() == {"detail": f"分级规则非法：{_problem(key)}"}
    assert "p21739_bad" not in _types(client, admin)


def test_改病种_同一判据_被拒的不写进去_预置规则照收(client, admin):
    """老库照运维手册补低血糖规则，`level3` 敲成 `level_3`：修前 PATCH 200，空腹血糖 2.8 判 1 级。"""
    diabetes = _types(client, admin)["diabetes"]
    high = diabetes["level_rules"]["metrics"][0]
    typo = {"key": "glucose", "name": "空腹血糖（低血糖）", "unit": "mmol/L", "direction": "low", "level_3": 3.9}
    url = f"{C}/disease-types/{diabetes['id']}"
    resp = client.patch(url, headers=admin, json={"level_rules": {"require_all": True, "metrics": [high, typo]}})
    assert resp.status_code == 422, resp.text   # 修前 200
    assert resp.json() == {"detail": f"分级规则非法：{_problem('glucose')}"}
    assert _types(client, admin)["diabetes"]["level_rules"] == diabetes["level_rules"]   # 被拒的那次一个字没写进去
    same = client.patch(url, headers=admin, json={"level_rules": diabetes["level_rules"]})
    assert same.status_code == 200 and same.json()["level_rules"] == diabetes["level_rules"], same.text


def test_契约用例的规则建病种照收(client, admin):
    resp = client.post(f"{C}/disease-types", headers=admin,
                       json={"code": "p21739_gout", "name": "P21739 痛风", "level_rules": GOUT_RULES})
    assert resp.status_code == 201 and resp.json()["level_rules"] == GOUT_RULES, resp.text


def test_存量没有阈值的规则_记随访与P1_125同一口径422(client, admin):
    """修前落库的这类规则：记随访修前 201、220/130 定 1 级；现在 422 点明病种与指标，随访不落库（不悄悄定成 1 级）。"""
    with SessionLocal() as db:
        db.add(ChronicDiseaseType(code="p21739_legacy", name="P21739 存量无阈值", guidance="", followup_interval_days=90,
                                  level_rules=NO_THRESHOLD))
        db.commit()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21739 慢病院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21739 患者", "id_card": "330106196501011739"}).json()["id"]
    chronic = client.post(C, headers=admin, json={"patient_id": patient, "disease": "p21739_legacy",
                                                  "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    chronic_id = chronic.json()["id"]
    resp = client.post(f"{C}/{chronic_id}/followups", headers=admin, json={"sbp": 220, "dbp": 130})
    assert resp.status_code == 422, resp.text   # 修前 201、定 1 级「控制良好」、不建议上转
    assert resp.json() == {"detail": f"病种「p21739_legacy」的分级规则配置有误（{_problem('sbp')}），请在病种目录里修正后再记随访"}
    with SessionLocal() as db:
        assert db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).count() == 0
