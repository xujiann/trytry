"""慢专病规则的「等于 / 不等于 / 属于 / 不属于」各算各的：数与文本对不上、列表整个拿去比（P2-342）。

`spd/rules.py` 说「只写一个求值器，四处共用——比较符语义只有一份」，实现里却是两种口径：`==` / `!=` 比 `str()`，
`in` / `not_in` 比 Python 的 `in`。而事实里的监测值是 float、数值题作答是 JSON 数、诊断与多选作答是列表；规则编辑器把
「属于」的值存成文本列表、「等于」的值能读成数就存成数。于是：

- `bp_sys == 140` 遇 140.0 不命中，`bp_sys != 140` 反而命中；
- `age in ["60", "65"]` 遇 60 岁不命中，`age not_in ["60"]` 命中；
- 问卷 `pain in ["8", "9", "10"]` 遇作答 8 判「无异常」，不派处置任务；
- 多选作答 `["无"]` 配 `症状 != "无"` 恒命中：误判异常，中重度还派处置任务。

修法：四个比较符共用一个等值口径——两边都读得成数就按数比，否则按去掉首尾空白的文本比；等于 / 不等于就是只有一个值的
属于 / 不属于，左值是列表时任一元素相等即算。
"""
import pytest

from app.spd.rules import evaluate, grade_abnormal

B = "/api/spd"


def _hit(field, op, value, facts):
    return evaluate([{"field": field, "op": op, "value": value}], facts)[0]


# ================================================================ 求值器
def test_监测值是float_等于与不等于按数比():
    assert _hit("bp_sys", "==", 140, {"bp_sys": 140.0}) is True     # 修前 False
    assert _hit("bp_sys", "!=", 140, {"bp_sys": 140.0}) is False    # 修前 True


def test_属于的值是文本列表_数值事实照样对得上():
    assert _hit("age", "in", ["60", "65"], {"age": 60}) is True        # 修前 False
    assert _hit("age", "not_in", ["60"], {"age": 60}) is False         # 修前 True
    assert _hit("gender", "in", [" 男 "], {"gender": "男"}) is True    # 文本去首尾空白再比


def test_多选作答是列表_等于不等于按元素算():
    assert _hit("symptom", "!=", "无", {"symptom": ["无"]}) is False      # 修前 True：误判异常
    assert _hit("symptom", "!=", "无", {"symptom": ["头晕"]}) is True
    assert _hit("symptom", "==", "头晕", {"symptom": ["头晕", "乏力"]}) is True   # 修前 False


def test_原有口径不变():
    assert _hit("age", "==", 50, {"age": "50"}) is True
    assert _hit("diagnosis", "in", ["I10", "E11"], {"diagnosis": ["J45", "I10"]}) is True
    assert _hit("diagnosis", "in", ["I10", "E11"], {"diagnosis": ["J45"]}) is False
    assert _hit("fever", "==", "是", {"fever": "否"}) is False


def test_问卷异常分级_作答是数_规则值是文本():
    rules = [{"when": {"field": "pain", "op": "in", "value": ["8", "9", "10"]}, "level": "high", "action": "通知主管医师"}]
    assert grade_abnormal(rules, {"pain": 8}) == ("high", "通知主管医师")   # 修前 ("none", "")


# ================================================================ 端点：执行随访判出异常、派处置任务
@pytest.fixture(scope="module")
def run(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2342 随访院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2342 患者", "id_card": "330106197001012342", "gender": "女", "birth_date": "1970-01-01"}).json()["id"]
    items = [{"key": "pain", "title": "疼痛评分", "type": "number"},
             {"key": "symptom", "title": "伴随症状", "type": "multi",
              "options": [{"label": "无"}, {"label": "头晕"}, {"label": "乏力"}]}]
    rules = [{"when": {"field": "pain", "op": "in", "value": ["8", "9", "10"]}, "level": "high", "action": "通知主管医师"},
             {"when": {"field": "symptom", "op": "!=", "value": "无"}, "level": "mid", "action": "电话随访"}]
    created = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P2342_Q", "name": "P2342 问卷", "items": items, "abnormal_rules": rules})
    assert created.status_code == 201, created.text
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P2342_R", "name": "P2342 随访方案", "points": [0, 7], "questionnaire_code": "P2342_Q"})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "base_date": "2001-01-01", "org_id": org})
    assert plan.status_code == 201, plan.text
    return {"patient": patient, "records": [item["id"] for item in plan.json()["items"]]}


def _tasks(patient):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter(SpdTask.patient_id == patient, SpdTask.source == "followup").count()


def test_执行随访_疼痛8分判重度派任务_多选答无不误判(client, admin, run):
    first, second = run["records"]
    done = client.post(f"{B}/followup-records/{first}/execute", headers=admin,
                       json={"answers": {"pain": 8, "symptom": ["无"]}})
    assert done.status_code == 200, done.text
    assert done.json()["abnormal_level"] == "high"          # 修前 mid：疼痛那条没命中，只命中了误判的症状那条
    assert _tasks(run["patient"]) == 1
    done = client.post(f"{B}/followup-records/{second}/execute", headers=admin,
                       json={"answers": {"pain": 2, "symptom": ["无"]}})
    assert done.status_code == 200, done.text
    assert done.json()["abnormal_level"] == "none"          # 修前 mid：答「无」被当成有症状，又派一条任务
    assert _tasks(run["patient"]) == 1
