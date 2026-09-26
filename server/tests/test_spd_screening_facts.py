"""筛查登记判纳入时，量表喂给规则的事实是错的（P2-368）。

`create_screening` 说「量表给风险等级（问卷答出来的），规则给纳入判定（诊断与指标推出来的）」。可判纳入时答案被整个盖在
事实上：种子「糖尿病高危筛查问卷」有一题键名就叫 `age`（「年龄≥45岁」），76 岁的患者年龄成了「是」，按年龄写的纳入规则
全不命中；题目键名叫 `diagnosis` 的还会把诊断整个换掉。另一头，规则字段表列着「量表得分」（`score`），可筛查算出了得分
却从不放进事实——按得分写的纳入规则永远不命中。

修法：答案只补库里推不出来的事实（`build_facts(answers=...)`）；用了量表的，得分记成事实 `score`。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2368 筛查卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _program(client, admin, code, rule, items, ranges=None):
    got = client.post(f"{B}/programs", headers=admin, json={
        "code": code, "name": f"{code} 病种", "include_rules": [rule]})
    assert got.status_code == 201, got.text
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": f"{code}_scale", "name": f"{code} 问卷", "category": "screen", "program_code": code, "items": items,
        "scoring": {"ranges": ranges or [{"min": 0, "max": None, "risk": "low", "advice": "保持"}]}})
    assert scale.status_code == 201, scale.text
    assert client.post(f"{B}/scales/{scale.json()['id']}/publish", headers=admin).status_code == 200
    return f"{code}_scale"


def _yes_no(key, title, score):
    return {"key": key, "title": title, "type": "single",
            "options": [{"label": "是", "score": score}, {"label": "否", "score": 0}]}


def _screen(client, admin, world, program, scale, answers, birth_date="1950-03-01"):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2368 患者{world['n']}", "id_card": f"33010419500301{world['n']:03d}8", "gender": "男",
        "birth_date": birth_date}).json()["id"]
    got = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": patient, "program_code": program, "org_id": world["org"], "source": "active",
        "scale_code": scale, "answers": answers})
    assert got.status_code == 201, got.text
    return got.json()


def test_题目键名与库里的事实同名_不盖掉库里的年龄(client, admin, world):
    scale = _program(client, admin, "p2368_age", {"field": "age", "op": ">=", "value": 60, "label": "60 岁以上"},
                     [_yes_no("age", "年龄≥45岁", 0)])
    got = _screen(client, admin, world, "p2368_age", scale, {"age": "是"})
    assert got["result"] == "suspect", got   # 修前 normal：年龄被答案盖成了「是」


def test_量表得分进事实_按得分写的纳入规则命中(client, admin, world):
    scale = _program(client, admin, "p2368_score", {"field": "score", "op": ">=", "value": 5, "label": "得分≥5"},
                     [_yes_no("q1", "头晕", 3), _yes_no("q2", "头痛", 3)])
    got = _screen(client, admin, world, "p2368_score", scale, {"q1": "是", "q2": "是"})
    assert (got["score"], got["risk_level"]) == (6, "low")
    assert got["result"] == "suspect", got   # 修前 normal：得分从不进事实
    low = _screen(client, admin, world, "p2368_score", scale, {"q1": "是", "q2": "否"})
    assert low["result"] == "normal", low


def test_库里推不出来的事实照旧由答案补上(client, admin, world):
    scale = _program(client, admin, "p2368_smoke", {"field": "smoking", "op": "==", "value": "是", "label": "吸烟"},
                     [_yes_no("smoking", "吸烟", 0)])
    assert _screen(client, admin, world, "p2368_smoke", scale, {"smoking": "是"})["result"] == "suspect"
    assert _screen(client, admin, world, "p2368_smoke", scale, {"smoking": "否"})["result"] == "normal"
