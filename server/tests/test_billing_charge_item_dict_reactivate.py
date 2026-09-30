"""启用四统一收费字典之后，停用的自编码收费项目还能再启用回目录（P2-923，第二十五批「配置改动的生效时点」扫描 J1-6）。

收费字典管控（`_charge_dict_blocked`）：字典导入了条目，只有字典内的编码能入目录——新建自编码项目 422。
可改档接口把 active 从 False 翻回 True 不过这道校验：「停用 → 再启用」，字典外的项目照样回到目录、照常计费。

修法：停用的再启用与新建同一道管控、同一句 422；已在用的项目改名改价照旧（存量先增量后存量，另有安排）。
"""
import pytest

B = "/api/billing"


def _stored(item_id):
    from app.database import SessionLocal
    from app.models import ChargeItem

    with SessionLocal() as db:
        item = db.get(ChargeItem, item_id)
        return item.active, item.price, item.name


@pytest.fixture(scope="module")
def items(client, admin):
    local = client.post(f"{B}/charge-items", headers=admin, json={
        "code": "P2923-LOCAL-CT", "name": "CT（本院自编码）", "category": "exam", "price": 200})
    assert local.status_code == 201, local.text   # 字典未启用：自编码照收
    kept = client.post(f"{B}/charge-items", headers=admin, json={
        "code": "P2923-LOCAL-DR", "name": "DR（本院自编码）", "category": "exam", "price": 80})
    assert kept.status_code == 201, kept.text
    imported = client.post("/api/dictionaries/charge/import", headers=admin, json=[
        {"code": "P2923-250101001", "name": "X线计算机体层(CT)平扫"}])
    assert imported.status_code in (200, 201), imported.text
    listed = client.post(f"{B}/charge-items", headers=admin, json={
        "code": "P2923-250101001", "name": "CT 平扫", "category": "exam", "price": 180})
    assert listed.status_code == 201, listed.text
    return {"local": local.json()["id"], "kept": kept.json()["id"], "listed": listed.json()["id"]}


def test_字典启用后_新建自编码项目照旧拦下(client, admin, items):
    resp = client.post(f"{B}/charge-items", headers=admin, json={
        "code": "P2923-LOCAL-MRI", "name": "MRI（自编码）", "category": "exam", "price": 500})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "编码不在四统一收费字典内"


def test_停用的自编码项目不能再启用回目录(client, admin, items):
    off = client.patch(f"{B}/charge-items/{items['local']}", headers=admin, json={"active": False})
    assert off.status_code == 200 and off.json()["active"] is False, off.text
    on = client.patch(f"{B}/charge-items/{items['local']}", headers=admin, json={"active": True, "price": 220})
    assert on.status_code == 422, on.text   # 修前 200：active 翻回 True，照常计费
    assert on.json()["detail"] == "编码不在四统一收费字典内"
    assert _stored(items["local"])[:2] == (False, 200)   # 连带的改价也没落下


def test_字典内的编码停用再启用照常(client, admin, items):
    assert client.patch(f"{B}/charge-items/{items['listed']}", headers=admin, json={"active": False}).status_code == 200
    on = client.patch(f"{B}/charge-items/{items['listed']}", headers=admin, json={"active": True})
    assert on.status_code == 200 and on.json()["active"] is True, on.text


def test_在用的自编码项目改名照旧(client, admin, items):
    """字典启用前建的、一直没停过的自编码项目，改名时连 active=True 一起送也照旧——管的是「重新进目录」，
    存量在用的另有「先增量后存量」的安排（docs/开发时间计划.md）。"""
    renamed = client.patch(f"{B}/charge-items/{items['kept']}", headers=admin,
                           json={"name": "DR（本院自编码，数字化）", "active": True})
    assert renamed.status_code == 200, renamed.text
    assert _stored(items["kept"]) == (True, 80, "DR（本院自编码，数字化）")
