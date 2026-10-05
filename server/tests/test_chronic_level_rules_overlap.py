"""分级规则同一指标挂了越高越危、越低越危两条时，两段不得相交（P2-1544，第四十五批扫描 AI2-6）。

`level_rules_problem` 原先只逐条查（P2-886 管单条规则内阈值与方向写反），同一指标的高、低两条之间不查。扫描实测（修前
代码）：空腹血糖挂「高 10/7」加「低 3.9/7.5」建病种 201；随访空腹血糖 5.5、6.1 都定 2 级「需干预」——低的 2 级阈值 7.5 压过
了高的 2 级阈值 7.0，落在中间的正常读数两头都够得着，永远到不了 1 级，正是这个函数自己说要挡的「悄悄定错级」。

修法：同一指标两个方向并存时，低侧最大的阈值须小于高侧最小的阈值，否则 422 并点名是哪两条；挂在 `level_rules_problem`
里，建病种与改病种两条路同一判据。修前存进去的这样的规则，记随访时与 P1-125 同一口径：422 说清楚、请在病种目录里改，随访
不落库（不悄悄跳过定级）。预置的糖尿病规则（低血糖一条只有 3 级阈值 3.9，P2-119）不相交，照常通过。
"""
import pytest

from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES
from app.routers.chronic import level_rules_problem

C = "/api/chronic"
HIGH = {"key": "glucose", "name": "空腹血糖", "unit": "mmol/L", "direction": "high", "level3": 10.0, "level2": 7.0}
LOW_OVERLAP = {"key": "glucose", "name": "空腹血糖（低血糖）", "unit": "mmol/L", "direction": "low", "level3": 3.9,
               "level2": 7.5}
OVERLAP = {"require_all": True, "metrics": [HIGH, LOW_OVERLAP]}
#: 扫描原样的那一组：低的 2 级阈值 7.5 不小于高的 2 级阈值 7
OVERLAP_DETAIL = ("指标 glucose 的越低越危「空腹血糖（低血糖）」2 级阈值 7.5 须小于越高越危「空腹血糖」2 级阈值 7："
                  "两段相交，落在中间的正常读数到不了 1 级")


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21544 慢病院", "org_type": "township", "level": "township"}).json()["id"]


def _archive(client, admin, org, disease, n):
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21544 患者{n}", "id_card": f"33010619650101544{n}"}).json()["id"]
    resp = client.post(C, headers=admin, json={"patient_id": patient, "disease": disease, "managed_by_org_id": org})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _level(client, admin, chronic_id, glucose):
    resp = client.post(f"{C}/{chronic_id}/followups", headers=admin, json={"glucose": glucose})
    assert resp.status_code == 201, resp.text
    return resp.json()["level"]


def test_判据_点名是哪两条():
    assert level_rules_problem(OVERLAP) == OVERLAP_DETAIL
    # 相等也算相交：7.0 同时落在「≥7 → 2 级」与「≤7 → 2 级」里，中间没有一个读数是 1 级
    touch = {"metrics": [HIGH, {**LOW_OVERLAP, "level2": 7.0}]}
    assert level_rules_problem(touch) == OVERLAP_DETAIL.replace("2 级阈值 7.5", "2 级阈值 7")
    # 低侧的 3 级阈值越过高侧的 3 级阈值（2 级没写），点名的是那两档
    assert level_rules_problem({"metrics": [{"key": "g", "name": "高", "direction": "high", "level3": 10.0},
                                            {"key": "g", "name": "低", "direction": "low", "level3": 12.0}]}) == (
        "指标 g 的越低越危「低」3 级阈值 12 须小于越高越危「高」3 级阈值 10：两段相交，落在中间的正常读数到不了 1 级")


def test_判据_不相交与单向的照常通过():
    assert level_rules_problem({"metrics": [HIGH, {**LOW_OVERLAP, "level2": 4.4}]}) == ""
    assert level_rules_problem({"metrics": [HIGH, {**LOW_OVERLAP, "key": "glucose_2h"}]}) == ""   # 不同指标不比
    assert level_rules_problem({"metrics": [HIGH, {**HIGH, "level3": 8.0, "level2": 6.0}]}) == ""  # 同向两条不比
    for seed in SEED_CHRONIC_DISEASE_TYPES:   # 预置的八个病种（含糖尿病高、低两条）照常通过
        assert level_rules_problem(seed["level_rules"]) == "", seed["code"]


def test_建病种_高低两段相交422(client, admin):
    resp = client.post(f"{C}/disease-types", headers=admin, json={
        "code": "p21544_dm", "name": "P21544 糖尿病（试）", "level_rules": OVERLAP})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": f"分级规则非法：{OVERLAP_DETAIL}"}


def test_改病种_同一判据_预置糖尿病规则照常通过(client, admin):
    types = {t["code"]: t for t in client.get(f"{C}/disease-types", headers=admin).json()}
    diabetes = types["diabetes"]
    resp = client.patch(f"{C}/disease-types/{diabetes['id']}", headers=admin, json={"level_rules": OVERLAP})
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": f"分级规则非法：{OVERLAP_DETAIL}"}
    same = client.patch(f"{C}/disease-types/{diabetes['id']}", headers=admin,
                        json={"level_rules": diabetes["level_rules"]})
    assert same.status_code == 200 and same.json()["level_rules"] == diabetes["level_rules"], same.text


def test_正常读数回到1级(client, admin, org):
    """预置糖尿病：16.7 定 3 级，5.5 回到 1 级；两段不相交的高低两条：低侧照样定得出 2、3 级。"""
    dm = _archive(client, admin, org, "diabetes", 0)
    assert [_level(client, admin, dm, g) for g in (16.7, 5.5, 3.5)] == [3, 1, 3]
    created = client.post(f"{C}/disease-types", headers=admin, json={
        "code": "p21544_ok", "name": "P21544 糖尿病（高低两条）",
        "level_rules": {"require_all": True, "metrics": [HIGH, {**LOW_OVERLAP, "level2": 4.4}]}})
    assert created.status_code == 201, created.text
    ok = _archive(client, admin, org, "p21544_ok", 1)
    assert [_level(client, admin, ok, g) for g in (5.5, 6.1, 4.0, 3.5, 7.5, 12.0)] == [1, 1, 2, 3, 2, 3]


def test_存量相交的规则_记随访与P1_125同一口径422(client, admin, org):
    from app.database import SessionLocal
    from app.models import ChronicDiseaseType, FollowUp

    with SessionLocal() as db:
        db.add(ChronicDiseaseType(code="p21544_legacy", name="P21544 存量相交", guidance="", followup_interval_days=90,
                                  level_rules=OVERLAP))
        db.commit()
    chronic = _archive(client, admin, org, "p21544_legacy", 2)
    resp = client.post(f"{C}/{chronic}/followups", headers=admin, json={"glucose": 5.5})
    assert resp.status_code == 422, resp.text   # 修前 201、定 2 级
    assert resp.json() == {"detail": f"病种「p21544_legacy」的分级规则配置有误（{OVERLAP_DETAIL}），请在病种目录里修正后再记随访"}
    with SessionLocal() as db:
        assert db.query(FollowUp).filter(FollowUp.chronic_id == chronic).count() == 0
