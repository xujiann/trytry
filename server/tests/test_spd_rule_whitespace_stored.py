"""问卷异常规则 / 分组自动规则按去掉空格后的字段查、却存原样：「pain 」过了校验，永远不命中（P2-290）。

`validate_conditions` 先把 field / op 去掉两端空格再判合法；病种纳入、路径条件、转诊触发三处存的是它的规范化结果，
问卷异常规则（`_check_abnormal_rules`）与分组自动规则（`create_group`，P1-123「只查不改写」）却存请求原样。求值
（`_match_one`）按存进去的原样比：字段「pain 」在作答里找不到、比较符「 >=」一个分支都进不去——疼痛 9 分记成
「无异常」、不派处置任务；按规则入组一个不中。都没有任何报错。

修法：查的是哪个就存哪个——只把 field / op 换成去掉空格后的值，其余键原样（干净的输入存进去的字节不变）；
分组按规则入组时按查过的样子求值，修前存下的带空格规则也照样命中。
"""
import pytest

B = "/api/spd"
PAIN = {"key": "pain", "title": "疼痛评分", "type": "number"}


def _padded_rule():
    return {"when": {"field": "pain ", "op": " >=", "value": 7}, "level": "high", "action": "通知主管医师"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2290 随访院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2290 患者", "id_card": "330106197001012290", "gender": "女", "birth_date": "1970-01-01"}).json()["id"]
    return {"org": org, "patient": patient}


def test_建问卷_带空格的规则字段存成查过的样子_执行随访判得出异常(client, admin, world):
    created = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P2290_Q", "name": "P2290 问卷", "items": [PAIN], "abnormal_rules": [_padded_rule()]})
    assert created.status_code == 201, created.text
    assert created.json()["abnormal_rules"] == [   # 修前原样：「pain 」「 >=」
        {"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high", "action": "通知主管医师"}]
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P2290_R", "name": "P2290 随访方案", "points": [0], "questionnaire_code": "P2290_Q"})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": world["patient"], "rule_id": rule.json()["id"], "base_date": "2001-01-01", "org_id": world["org"]})
    assert plan.status_code == 201, plan.text
    (record,) = (item["id"] for item in plan.json()["items"])
    done = client.post(f"{B}/followup-records/{record}/execute", headers=admin, json={"answers": {"pain": 9}})
    assert done.status_code == 200, done.text
    assert done.json()["abnormal_level"] == "high"   # 修前 none：疼痛 9 分记成无异常


def test_改问卷_带空格的规则同样存成查过的样子(client, admin):
    created = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P2290_Q2", "name": "P2290 问卷二", "items": [PAIN], "abnormal_rules": []})
    assert created.status_code == 201, created.text
    patched = client.patch(f"{B}/questionnaires/{created.json()['id']}", headers=admin,
                           json={"abnormal_rules": [_padded_rule()]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["abnormal_rules"][0]["when"] == {"field": "pain", "op": ">=", "value": 7}


def test_干净的规则存进去字节不变(client, admin):
    """只换 field / op 两个键：不多出 label 之类的规范化键，出参与请求一致。"""
    rules = [{"when": {"field": "pain", "op": "between", "value": [5, 7]}, "level": "mid", "action": "电话追访"}]
    created = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P2290_Q3", "name": "P2290 问卷三", "items": [PAIN], "abnormal_rules": rules})
    assert created.status_code == 201 and created.json()["abnormal_rules"] == rules, created.text
    group = client.post(f"{B}/groups", headers=admin, json={
        "name": "P2290 干净分组", "auto_rule": [{"field": "risk_level", "op": "in", "value": ["high"]}]})
    assert group.status_code == 201, group.text
    row = next(g for g in client.get(f"{B}/groups", headers=admin).json() if g["id"] == group.json()["id"])
    assert row["auto_rule"] == [{"field": "risk_level", "op": "in", "value": ["high"]}]


def _enroll(client, admin, world):
    r = client.post(f"{B}/enrollments", headers=admin,
                    json={"patient_id": world["patient"], "program_code": "hypertension", "org_id": world["org"]})
    assert r.status_code in (201, 409), r.text


def test_建分组_带空格的自动规则存成查过的样子_按规则入组命中(client, admin, world):
    _enroll(client, admin, world)
    group = client.post(f"{B}/groups", headers=admin, json={
        "name": "P2290 分组", "auto_rule": [{"field": " risk_level", "op": "exists ", "value": True}]})
    assert group.status_code == 201, group.text
    row = next(g for g in client.get(f"{B}/groups", headers=admin).json() if g["id"] == group.json()["id"])
    assert row["auto_rule"] == [{"field": "risk_level", "op": "exists", "value": True}]   # 修前原样
    added = client.post(f"{B}/groups/{group.json()['id']}/members", headers=admin,
                        json={"use_auto_rule": True, "program_code": "hypertension"})
    assert added.status_code == 200, added.text
    assert added.json()["added"] >= 1   # 修前 0：一个不中


def test_存量带空格的分组规则_按规则入组照样命中(client, admin, world):
    """修前存下的原样规则不改库，按规则入组时按查过的样子求值。"""
    from app.database import SessionLocal
    from app.models import User
    from app.spd.models import SpdGroup

    _enroll(client, admin, world)
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        legacy = SpdGroup(name="P2290 存量分组", owner_user_id=admin_id, org_id=None,
                          auto_rule=[{"field": "risk_level ", "op": " exists", "value": True}])
        db.add(legacy)
        db.commit()
        group_id = legacy.id
    added = client.post(f"{B}/groups/{group_id}/members", headers=admin,
                        json={"use_auto_rule": True, "program_code": "hypertension"})
    assert added.status_code == 200, added.text
    assert added.json()["added"] >= 1   # 修前 0
