"""随访问卷的异常判定规则写得进、判不出（P1-122）。

执行随访按题目 key 把作答交给 `grade_abnormal` 求值：规则引用问卷里没有的题目（`pain` 写成 `pian`）、
级别写成表外的值，建问卷照样 201，却永远判不出异常——疼痛 9 分记成「无异常」、不派处置任务，没有任何报错。
改问卷时只改题目、删掉规则引用的那道题，同样照收。

修法：建 / 改问卷把题目与规则合起来查——题目必须有 key 且不重复，规则字段必须是本问卷的题目，级别只能是
low / mid / high；改问卷时题目与规则都没变不查（存量里已写坏的问卷，改名、停用不挡）。
"""
import pytest

B = "/api/spd"
PAIN = {"key": "pain", "title": "疼痛评分", "type": "number"}
FEVER = {"key": "fever", "title": "是否发热", "type": "single", "options": [{"label": "否"}, {"label": "是"}]}


def _rule(field="pain", op=">=", value=7, level="high"):
    return {"when": {"field": field, "op": op, "value": value}, "level": level, "action": "通知主管医师"}


def _create(client, admin, code, items, rules):
    return client.post(f"{B}/questionnaires", headers=admin,
                       json={"code": code, "name": f"{code} 问卷", "items": items, "abnormal_rules": rules})


@pytest.mark.parametrize(("items", "rules", "detail"), [
    ([PAIN], [_rule(field="pian")], "异常分级规则引用了问卷里没有的题目：pian"),
    ([], [_rule()], "异常分级规则引用了问卷里没有的题目：pain"),
    ([PAIN], [_rule(level="severe")], "异常分级规则的级别只能是 low（轻度）/ mid（中度）/ high（重度）"),
    ([PAIN], [_rule(level="none")], "异常分级规则的级别只能是 low（轻度）/ mid（中度）/ high（重度）"),
    ([{"title": "没写 key 的题"}], [], "问卷的每道题都要有 key"),
    ([{"key": " ", "title": "key 是空白"}], [], "问卷的每道题都要有 key"),
    ([PAIN, {**PAIN, "title": "重复的 key"}], [], "问卷题目 key 不得重复"),
], ids=["规则引用不存在的题目", "没有题目却写规则", "级别表外", "级别写成none", "题目缺key", "key是空白", "key重复"])
def test_建问卷_规则判不出的写法一律422(client, admin, items, rules, detail):
    """修前七种写法全部 201：规则照存，执行随访时永远不命中。"""
    resp = _create(client, admin, "P122_BAD", items, rules)
    assert resp.status_code == 422 and resp.json() == {"detail": detail}, resp.text[:300]


def test_合法问卷照收_级别缺省按轻度(client, admin):
    rules = [_rule(), {"when": {"field": "fever", "op": "==", "value": "是"}, "action": "测体温"}]
    resp = _create(client, admin, "P122_OK", [PAIN, FEVER], rules)
    assert resp.status_code == 201, resp.text
    assert resp.json()["abnormal_rules"] == rules


def test_改问卷只改题目也查_删掉规则引用的题目422_不留半截(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdQuestionnaire

    created = _create(client, admin, "P122_EDIT", [PAIN, FEVER], [_rule()])
    assert created.status_code == 201, created.text
    url = f"{B}/questionnaires/{created.json()['id']}"
    resp = client.patch(url, headers=admin, json={"items": [FEVER]})   # 修前 200：规则还引用着 pain
    assert resp.status_code == 422 and resp.json() == {"detail": "异常分级规则引用了问卷里没有的题目：pain"}
    resp = client.patch(url, headers=admin, json={"abnormal_rules": [_rule(level="urgent")]})
    assert resp.status_code == 422, resp.text
    with SessionLocal() as db:
        row = db.get(SpdQuestionnaire, created.json()["id"])
        assert (row.items, row.abnormal_rules) == ([PAIN, FEVER], [_rule()])
    # 题目与规则一起改成彼此对得上的，照收
    resp = client.patch(url, headers=admin, json={"items": [FEVER], "abnormal_rules": [_rule("fever", "==", "是", "mid")]})
    assert resp.status_code == 200, resp.text


def test_存量写坏的问卷_改名停用不挡(client, admin):
    """修前落库的问卷可能已经写坏：题目与规则都没变的改档（改名、换跟踪科室、停用）不查它们。"""
    from app.database import SessionLocal
    from app.spd.models import SpdQuestionnaire

    with SessionLocal() as db:
        legacy = SpdQuestionnaire(code="P122_LEGACY", name="存量问卷", items=[PAIN], abnormal_rules=[_rule("pian")])
        db.add(legacy)
        db.commit()
        qid = legacy.id
    resp = client.patch(f"{B}/questionnaires/{qid}", headers=admin,
                        json={"name": "存量问卷（改名）", "track_dept": "外科", "active": False})
    assert resp.status_code == 200, resp.text
    resp = client.patch(f"{B}/questionnaires/{qid}", headers=admin, json={"items": [PAIN, FEVER]})
    assert resp.status_code == 422, resp.text   # 真要动题目或规则，连同存量的坏规则一起报出来


def test_执行随访按题目判出异常_中重度派处置任务(client, admin):
    """合法问卷走一遍执行：疼痛 9 分判重度、派一条处置任务；没答的题不参与判定。"""
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P122 随访院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P122 患者", "id_card": "330106197001011467", "gender": "女", "birth_date": "1970-01-01"}).json()["id"]
    assert _create(client, admin, "P122_RUN", [PAIN, FEVER],
                   [_rule(), _rule("fever", "==", "是", "mid")]).status_code == 201
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P122_RUN_R", "name": "P122 随访方案", "points": [0, 7], "questionnaire_code": "P122_RUN"})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "base_date": "2001-01-01", "org_id": org})
    assert plan.status_code == 201, plan.text
    first, second = (item["id"] for item in plan.json()["items"])

    def _tasks():
        with SessionLocal() as db:
            return db.query(SpdTask).filter(SpdTask.patient_id == patient, SpdTask.source == "followup").count()

    done = client.post(f"{B}/followup-records/{first}/execute", headers=admin, json={"answers": {"pain": 9}})
    assert done.status_code == 200 and done.json()["abnormal_level"] == "high", done.text
    assert _tasks() == 1
    done = client.post(f"{B}/followup-records/{second}/execute", headers=admin, json={"answers": {"pain": 2}})
    assert done.status_code == 200 and done.json()["abnormal_level"] == "none", done.text
    assert _tasks() == 1


def test_种子问卷都过得了同一道校验():
    """预置问卷不走接口，直接落库——同一道校验在这里钉住，种子改坏了先红。"""
    from app.spd.routers.followup import _check_abnormal_rules
    from app.spd.seed import SEED_QUESTIONNAIRES

    assert SEED_QUESTIONNAIRES
    for q in SEED_QUESTIONNAIRES:
        _check_abnormal_rules(q.get("abnormal_rules", []), q.get("items", []))
