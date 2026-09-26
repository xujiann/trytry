"""智能导诊：命中任一急症症状就提示急诊，急症科室排在最前（P2-128）。

原先推荐只按命中症状数排、急诊提示只看排第一的科室：「咳嗽、发热、胸痛」呼吸内科命中两个排第一，胸痛所在的
心血管内科排第二——急诊提示没有；急症科室排到第四，连推荐里都看不见。胸痛、头痛这类急症症状不该被别的症状
「票数」压下去。
"""


def _suggest(client, admin, symptoms):
    resp = client.post("/api/triage/suggest", json=symptoms, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_急症症状被别的症状压过票数_照样提示急诊且排第一(client, admin):
    body = _suggest(client, admin, ["咳嗽", "发热", "胸痛"])
    assert body["emergency_hint"] is True   # 修前 False
    assert [r["department"] for r in body["recommendations"]] == ["心血管内科", "呼吸内科"]   # 修前呼吸内科在前
    assert body["recommendations"][0] == {"department": "心血管内科", "matched": ["胸痛"], "urgent": True}


def test_急症科室原先排第四_推荐里看不见(client, admin):
    body = _suggest(client, admin, ["咳嗽", "咳痰", "腹痛", "腹泻", "尿频", "尿急", "胸痛"])
    assert body["emergency_hint"] is True   # 修前 False
    assert body["recommendations"][0]["department"] == "心血管内科"   # 修前不在前三
    assert len(body["recommendations"]) == 3


def test_没有急症症状_按命中数排_同数按知识库顺序(client, admin):
    body = _suggest(client, admin, ["腹泻", "咳嗽", "咳痰"])
    assert body["emergency_hint"] is False
    assert [r["department"] for r in body["recommendations"]] == ["呼吸内科", "消化内科"]


def test_几个急症科室之间仍按命中数排(client, admin):
    body = _suggest(client, admin, ["胸痛", "头晕", "头痛", "咳嗽"])
    assert body["emergency_hint"] is True
    assert [r["department"] for r in body["recommendations"]] == ["神经内科", "心血管内科", "呼吸内科"]
