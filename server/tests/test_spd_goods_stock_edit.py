"""积分商品「编辑」不再把页面加载时的库存整值写回（P2-920，第二十五批「占用型资源的释放」扫描 J2-4）。

兑换用 `take_amount` 条件扣减库存；改档 `GoodsPatch.stock` 是绝对值、直接 `setattr`，页面编辑框四格原样 PATCH（改名、
下架、补货都带着这个库存值）。页面打开时库存 3，村医兑换 1 件后库存 2，主任只改名，库存被写回 3；之后再兑 3 次全部成功、
库存 0——待核销单 4 张、实物 3 件，多兑的那位村医积分已扣。与缺药阈值 P1-146 同形。修后页面只送改过的字段；改库存时
带上页面看到的库存（`stock_seen`），库存还是那个数才改、否则 409；不带的旧调用照旧直接改。
"""
from pathlib import Path

import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2920 村卫生室", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/users", headers=admin, json={
        "username": "p2920_doc", "password": "passw0rd1", "full_name": "P2920 村医", "role": "doctor", "org_id": org})
    assert made.status_code in (200, 201), made.text
    doctor = _login(client, "p2920_doc")
    rule = next(r for r in client.get(f"{B}/point-rules", headers=admin).json() if r["event"] == "signin")
    assert client.patch(f"{B}/point-rules/{rule['id']}", headers=admin, json={"points": 10}).status_code == 200
    assert client.post(f"{B}/point-accounts/signin", headers=doctor).status_code in (200, 201)
    goods = client.post(f"{B}/goods", headers=admin, json={"code": "P2920CUP", "name": "保温杯", "points": 2, "stock": 3})
    assert goods.status_code == 201, goods.text
    return {"doctor": doctor, "goods": goods.json()["id"]}


def _stock(client, admin, goods):
    return next(g for g in client.get(f"{B}/goods", headers=admin).json() if g["id"] == goods)["stock"]


def test_页面看到的库存已变_改库存409_只改名不动库存(client, admin, world):
    goods = world["goods"]
    seen = _stock(client, admin, goods)   # 页面打开时 3
    assert client.post(f"{B}/redeems", headers=world["doctor"], json={"goods_id": goods}).status_code == 201
    stale = client.patch(f"{B}/goods/{goods}", headers=admin, json={"stock": seen + 5, "stock_seen": seen})
    assert stale.status_code == 409, stale.text   # 修前（不带期望值）按旧数写回，兑换占掉的件数被还回去
    assert _stock(client, admin, goods) == seen - 1
    renamed = client.patch(f"{B}/goods/{goods}", headers=admin, json={"name": "保温杯（500ml）", "active": True})
    assert renamed.status_code == 200 and renamed.json()["stock"] == seen - 1
    fresh = client.patch(f"{B}/goods/{goods}", headers=admin, json={"stock": 10, "stock_seen": seen - 1})
    assert fresh.status_code == 200 and fresh.json()["stock"] == 10


def test_页面只送改过的字段():
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
    start = source.index("if (goodsEdit) {")
    handler = source[start:source.index("\n    }", start)]
    assert "stock: form.stock, active" not in handler   # 修前四格原样送
    assert "stock_seen: Number(d.stock)" in handler and "String(form.stock) !== String(d.stock)" in handler
