"""积分兑换的核销码只给兑换人本人看（P0-41）。

核销码是线下领奖品的凭证：兑换人到点位出示，经办按码核销（`POST /api/spd/redeems/verify`），
核销时只认码、不认人，回执里也没有兑换人。可兑换清单（`GET /api/spd/redeems`）不带 `mine=true`
时返回全县每一张兑换单，**连同核销码**，任一登录账号都能取：2026-09-24 实测，别家卫生院的医生
列出清单就拿到了村医刚兑换、还没领走的核销码——拿着它去点位，经办照码核销，奖品就被领走了，
兑换人再去时只剩「核销码无效或已核销」。管理页的兑换表本就不显示核销码（只列商品、积分、状态、时间），
清单里带着它没有任何用处。

修法：清单里只有兑换人本人的单子带核销码，别人的一律打码（`******`）——经办与管理员也一样：
核销靠兑换人出示，不靠清单。兑换回执与本人清单照旧给码。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def redeem_world(client):
    """甲卫生院一位医生签到得 1 分、兑换一件 1 分的奖品；乙卫生院医生与本院经办各一名。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "核销码甲卫生院"), ("b", "核销码乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    for username, role, key in (("p041_doc_a", "doctor", "a"), ("p041_op_a", "operator", "a"),
                                ("p041_doc_b", "doctor", "b")):
        r = client.post("/api/users",
                        json={"username": username, "password": "pw123456", "full_name": username,
                              "role": role, "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    h = {u: _login(client, u) for u in ("p041_doc_a", "p041_op_a", "p041_doc_b")}
    goods = client.post("/api/spd/goods", json={"code": "p041_towel", "name": "核销码毛巾", "points": 1, "stock": 5},
                        headers=admin)
    assert goods.status_code == 201, goods.text
    signed = client.post("/api/spd/point-accounts/signin", headers=h["p041_doc_a"])
    assert signed.status_code == 200, signed.text
    redeemed = client.post("/api/spd/redeems", json={"goods_id": goods.json()["id"]}, headers=h["p041_doc_a"])
    assert redeemed.status_code == 201, redeemed.text
    return {"admin": admin, "h": h, "redeem_id": redeemed.json()["id"], "code": redeemed.json()["verify_code"]}


def _row(client, headers, redeem_id, query=""):
    r = client.get(f"/api/spd/redeems?limit=500{query}", headers=headers)
    assert r.status_code == 200, r.text
    return next(row for row in r.json() if row["id"] == redeem_id)


@pytest.mark.parametrize("who", ["p041_doc_b", "p041_op_a"])
def test_别人列清单拿不到核销码(client, redeem_world, who):
    row = _row(client, redeem_world["h"][who], redeem_world["redeem_id"])
    assert row["verify_code"] == "******", row
    assert row["status"] == "pending"


def test_管理员列清单也拿不到别人的核销码(client, redeem_world):
    """核销靠兑换人出示，不靠清单——管理页的兑换表本就不显示这一列。"""
    assert _row(client, redeem_world["admin"], redeem_world["redeem_id"])["verify_code"] == "******"


@pytest.mark.parametrize("query", ["", "&mine=true"])
def test_兑换人本人照常看得到自己的核销码(client, redeem_world, query):
    row = _row(client, redeem_world["h"]["p041_doc_a"], redeem_world["redeem_id"], query)
    assert row["verify_code"] == redeem_world["code"]


def test_凭兑换人出示的码照常核销(client, redeem_world):
    r = client.post("/api/spd/redeems/verify", json={"verify_code": redeem_world["code"]},
                    headers=redeem_world["h"]["p041_op_a"])
    assert r.status_code == 200, r.text
    assert r.json() == {"id": redeem_world["redeem_id"], "status": "verified"}
