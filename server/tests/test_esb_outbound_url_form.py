"""集成平台出站接入方的投递地址在界面上配得了（P2-860，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-9）。

`EndpointCreate` / `EndpointUpdate` 早就收 `endpoint_url`（留空即「仅登记」：消费成功但不投递）；页面的注册表单只送编码、
名称、系统类型、方向、限流，行上只有启停与轮换令牌——界面建的出站接入方全部「仅登记不投递」，消息却记成「已成功」。
修后注册表单带投递地址，清单显示投递地址（出站没配的标「未配」），出站行上给「改投递地址」。签名密钥（可选，仅入库不
回显）仍只经接口配。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


def _render_esb():
    start = PAGE.index("async function renderEsb()")
    return PAGE[start:PAGE.index("\nasync function ", start + 10)]


def test_注册表单带投递地址():
    body = _render_esb()
    start = body.index('<form class="inline" id="esb-ep-form">')
    assert '<input name="endpoint_url"' in body[start:body.index("</form>", start)]   # 修前没有
    assert "body.endpoint_url = String(f.get(\"endpoint_url\")).trim();" in body


def test_清单显示投递地址_出站行能改():
    body = _render_esb()
    assert '"方向", "投递地址", "限流/分钟"' in body and "未配（仅登记不投递）" in body
    assert 'data-esburl="${e.id}"' in body
    assert "/api/esb/endpoints/${esburl}" in body and "endpoint_url: String(form.endpoint_url).trim()" in body


def test_按页面送的地址注册_出站消息真投递(client, admin):
    made = client.post("/api/esb/endpoints", headers=admin, json={
        "code": "P2860_OUT", "name": "P2860 省平台", "system_type": "provincial", "direction": "outbound",
        "rate_limit_per_min": 60, "endpoint_url": "https://prov.example.gov.cn/esb"})
    assert made.status_code == 201, made.text
    assert made.json()["endpoint_url"] == "https://prov.example.gov.cn/esb"
    changed = client.patch(f"/api/esb/endpoints/{made.json()['id']}", headers=admin, json={
        "endpoint_url": "https://prov2.example.gov.cn/esb"})
    assert changed.status_code == 200 and changed.json()["endpoint_url"] == "https://prov2.example.gov.cn/esb"
