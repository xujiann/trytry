"""消毒供应申领的数量框清空后不送、取缺省 1（P2-865，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-14）。

`CssdReqCreate.quantity` 缺省 1、`ge=1`；页面按 `Number(f.get("quantity"))` 送，数量框清空时 `Number("")` 是 0，用户拿到的是
一句英文 422（`Input should be greater than or equal to 1`）。同文件号源的 capacity 早按「空就不送」修过。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")


def test_申领数量清空不送():
    start = PAGE.index('$("#creq-form").onsubmit')
    body = PAGE[start:PAGE.index("};", start)]
    assert '...(f.get("quantity") ? { quantity: Number(f.get("quantity")) } : {})' in body
    assert 'quantity: Number(f.get("quantity")) }) });' not in body   # 修前清空送 0


def test_不送数量_按缺省1申领(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2865 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/cssd/requests", headers=admin, json={"org_id": org, "item_name": "P2865 换药包"})
    assert made.status_code == 201, made.text
    zero = client.post("/api/cssd/requests", headers=admin, json={"org_id": org, "item_name": "P2865 换药包", "quantity": 0})
    assert zero.status_code == 422   # 修前页面送的正是这个
