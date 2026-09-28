"""居民端自查量表清单与扫码入口不再列停用病种的筛查量表（第十五批「停用对象仍被引用」扫描 S1-8）。

居民自查提交按 `unknown_program(active_only=True)` 拒停用病种（P1-89）；清单（`GET /api/portal/spd/scales`）与扫码入口
（`/scales/by-token/{令牌}`）只筛「已发布 + 筛查类」——病种停用后它的量表照列，居民答完一提交（连草稿试算也算）就 404
「专病档案不存在或已停用」。管理端的筛查表单早已随「只列启用病种」联动（P1-89）。修后两处都排除挂着停用病种的量表，
扫码的按「二维码无效或已失效」404（居民端据此提示码已失效、回落到可自查的清单）；不挂病种的通用量表照列。
"""
import pytest

B = "/api/spd"
P = "/api/portal/spd"
ITEMS = [{"key": "q1", "title": "是否头晕", "options": [{"label": "否", "score": 0}, {"label": "是", "score": 2}]}]


@pytest.fixture(scope="module")
def world(client, admin):
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": "s18_prog", "name": "S1-8 病种", "category": "chronic"})
    assert program.status_code == 201, program.text
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "s18_screen", "name": "S1-8 自查量表", "category": "screen", "program_code": "s18_prog", "items": ITEMS})
    assert scale.status_code == 201, scale.text
    published = client.post(f"{B}/scales/{scale.json()['id']}/publish", headers=admin)
    assert published.status_code == 200, published.text
    return {"program": program.json()["id"], "token": published.json()["qr_token"]}


def test_病种启用时照常列出_停用后清单与扫码都不再给(client, admin, world):
    def codes():
        return [s["code"] for s in client.get(f"{P}/scales").json()]

    assert "s18_screen" in codes()
    assert client.get(f"{P}/scales/by-token/{world['token']}").status_code == 200
    resp = client.patch(f"{B}/programs/{world['program']}", headers=admin, json={"active": False})
    assert resp.status_code == 200 and resp.json()["active"] is False, resp.text
    try:
        assert "s18_screen" not in codes()                                             # 修前照列，答完提交 404
        assert client.get(f"{P}/scales?program_code=s18_prog").json() == []
        gone = client.get(f"{P}/scales/by-token/{world['token']}")
        assert (gone.status_code, gone.json()) == (404, {"detail": "二维码无效或已失效"})   # 修前 200
    finally:
        client.patch(f"{B}/programs/{world['program']}", headers=admin, json={"active": True})
    assert "s18_screen" in codes()   # 重新启用即回来
