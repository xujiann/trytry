"""发布过的量表停用之后也不许改题目与评分，要改请新建版本（P2-151）。

改量表的接口写「已发布量表不可改题目或评分，请新建版本」，判的却只是**当下**是不是已发布：先停用，题目与评分
就能随便改，再发布回去——同一版本号、同一张印出去的二维码背后换了一份题目与评分，按原题算分的历次评估
与现在的量表对不上，查不出这份分数当初是按哪套题算的。
"""
import pytest

B = "/api/spd"
ITEMS = [{"key": "q1", "title": "头晕", "type": "single",
          "options": [{"label": "否", "score": 0}, {"label": "是", "score": 5}]}]
SCORING = {"ranges": [{"min": 0, "max": 2, "risk": "low", "advice": "保持"},
                      {"min": 3, "max": None, "risk": "high", "advice": "尽快复核"}]}


@pytest.fixture(scope="module")
def scale(client, admin):
    created = client.post(f"{B}/scales", headers=admin, json={
        "code": "p2151_scale", "name": "P2151 量表", "category": "screen", "items": ITEMS, "scoring": SCORING})
    assert created.status_code == 201, created.text
    return created.json()


def test_发布过再停用_照样不许改题目与评分(client, admin, scale):
    assert client.post(f"{B}/scales/{scale['id']}/publish", headers=admin).status_code == 200
    assert client.post(f"{B}/scales/{scale['id']}/disable", headers=admin).json()["status"] == "disabled"
    changed_items = [{**ITEMS[0], "options": [{"label": "否", "score": 0}, {"label": "是", "score": 1}]}]
    for body in ({"items": changed_items}, {"scoring": {"ranges": [{"min": 0, "max": None, "risk": "low"}]}}):
        resp = client.patch(f"{B}/scales/{scale['id']}", headers=admin, json=body)
        assert resp.status_code == 409, resp.text   # 修前 200：停用后随便改，再发布回同一版本
        assert "新建版本" in resp.json()["detail"]
    detail = client.get(f"{B}/scales/{scale['id']}", headers=admin).json()
    assert (detail["items"], detail["scoring"]) == (ITEMS, SCORING)


def test_停用的量表改名照常_草稿照常改题(client, admin, scale):
    renamed = client.patch(f"{B}/scales/{scale['id']}", headers=admin, json={"name": "P2151 量表（旧版）"})
    assert renamed.status_code == 200, renamed.text
    draft = client.post(f"{B}/scales", headers=admin, json={
        "code": "p2151_draft", "name": "P2151 草稿", "category": "screen", "items": ITEMS, "scoring": SCORING}).json()
    assert client.post(f"{B}/scales/{draft['id']}/disable", headers=admin).status_code == 200   # 没发布过就停用
    edited = client.patch(f"{B}/scales/{draft['id']}", headers=admin, json={"items": ITEMS + [
        {"key": "q2", "title": "乏力", "type": "single", "options": [{"label": "否", "score": 0}]}]})
    assert edited.status_code == 200, edited.text
