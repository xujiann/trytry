"""缺药统计的「在途」把已配送也算进去，与同一文件里「在途 = 药还没到」的口径相反（P2-353）。

`shortage_stats` 的说明写「履约率的分母……不含在途与已取消——药还没到就算进分母」，`_SHORTAGE_SHORT` 写「还缺着的：
药还在路上（已登记 / 采购中）。已配送的药已经到了」；`in_transit` 却是已登记 + 采购中 + 已配送，页面卡片「在途」多数一截。
顺带：推进一条已配送的登记报「状态 已配送 已是终态」——它不是终态（还要结案），页面也照样给它摆着结案按钮。
"""
B = "/api/medication"


def _shortage(client, admin, org, steps):
    got = client.post(f"{B}/shortages", headers=admin, json={
        "org_id": org, "drug_code": "P2353", "drug_name": "P2353 片", "quantity": 1})
    assert got.status_code in (200, 201), got.text
    sid = got.json()["id"]
    for _ in range(steps):
        assert client.post(f"{B}/shortages/{sid}/advance", headers=admin).status_code == 200
    return sid


def test_在途只算药还在路上的(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2353 缺药卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for steps in (0, 1, 2):   # 已登记 / 采购中 / 已配送
        _shortage(client, admin, org, steps)
    stats = client.get(f"{B}/shortages/stats", headers=admin).json()
    assert stats["by_status"] == {"delivered": 1, "purchasing": 1, "registered": 1}
    assert stats["in_transit"] == 2   # 修前 3：已配送的也算在途


def test_推进已配送的登记_报请结案而不是已是终态(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2353 配送卫生院", "org_type": "township", "level": "township"}).json()["id"]
    sid = _shortage(client, admin, org, 2)
    resp = client.post(f"{B}/shortages/{sid}/advance", headers=admin)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "已配送的登记不能再推进，请结案"}   # 修前「状态 已配送 已是终态」
    closed = client.post(f"{B}/shortages/{sid}/close", headers=admin, json={"result": "cancelled", "reason": "药源已解决"})
    assert closed.status_code == 200, closed.text
    resp = client.post(f"{B}/shortages/{sid}/advance", headers=admin)
    assert resp.json() == {"detail": "状态 已取消 已是终态"}   # 真正的终态照旧
