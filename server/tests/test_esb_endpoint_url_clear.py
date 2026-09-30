"""ESB 出站接入方的投递地址改得回「仅登记不投递」（P2-1085，第三十一批「页面输入约束 vs 后端校验」扫描 C3-7）。

后端的注释写「留空即『仅登记不投递』照旧」，改档接口也收空串；页面「改投递地址」弹窗却把这一项设成必填——设过投递
地址的接入方在下游停用或维护时，界面上改不回仅登记，只能停用整个接入方或直接调接口。修后弹窗不必填、标签写明留空的意思。
"""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


def test_改投递地址的弹窗不必填_写明留空的意思():
    start = SOURCE.index('spdModal("改出站投递地址"')
    field = SOURCE[start:SOURCE.index("]);", start)]
    assert "required: true" not in field          # 修前必填
    assert "留空 = 仅登记不投递" in field


def test_后端改成空串即仅登记不投递(client, admin):
    created = client.post("/api/esb/endpoints", headers=admin, json={
        "code": "P21085", "name": "P21085 出站", "system_type": "his", "direction": "outbound", "endpoint_url": "https://example.com/hook"})
    assert created.status_code == 201, created.text
    resp = client.patch(f"/api/esb/endpoints/{created.json()['id']}", headers=admin, json={"endpoint_url": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["endpoint_url"] == ""
