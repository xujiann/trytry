"""兑换核销码在待核销单之间判重：撞码时一个码不再核得了两次（P2-1578，第四十六批扫描 AJ4-2）。

兑换原先 `verify_code=f"{randbelow(1000000):06d}"` 随手一抽、不查待核销单里有没有同码的；核销按码加待核销状态取
`.first()`、不排序。扫描实测（修前代码，随机数恒为 123456 模拟自然撞码）：村医甲兑保温杯、村医乙兑电饭煲，两张单的码
都是 123456；经办核 123456 回 `{'id': 1, 'status': 'verified'}`，甲再出示一次回 `{'id': 2, 'status': 'verified'}`——
核掉的是乙的电饭煲，第三次才 404。乙 400 分已扣，到点位只得到「核销码无效或已核销」。同时有 N 张待核销单时新兑一单
撞码的概率约 N/10⁶。

修法：出码时在待核销单里判重，撞了重抽，连抽 `VERIFY_CODE_ATTEMPTS` 次都撞上 409 让人重试（库存与积分未扣）；核销取单
补 `order_by(SpdRedeem.id)`，存量里万一已有同码的待核销单，按兑换先后核、结果确定。不加唯一索引、不改存量数据。
"""
import ast
from pathlib import Path

import pytest

from conftest import login

import app.spd.routers.assess as assess

B = "/api/spd"
SOURCE = Path(assess.__file__)


@pytest.fixture(scope="module")
def world(client, admin):
    """一家村卫生室的两位村医（签到各得 500 分）、一位经办；保温杯 50 分、电饭煲 400 分，库存都够。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21578 村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    for username, role in (("p21578_va", "doctor"), ("p21578_vb", "doctor"), ("p21578_op", "operator")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": username})
        assert created.status_code == 201, created.text
    heads = {u: login(client, u, "passw0rd1") for u in ("p21578_va", "p21578_vb", "p21578_op")}
    signin_rule = next(r for r in client.get(f"{B}/point-rules", headers=admin).json() if r["event"] == "signin")
    assert client.patch(f"{B}/point-rules/{signin_rule['id']}", headers=admin, json={"points": 500}).status_code == 200
    for doctor in ("p21578_va", "p21578_vb"):
        assert client.post(f"{B}/point-accounts/signin", headers=heads[doctor]).status_code == 200
    goods = {}
    for code, name, points in (("p21578_cup", "保温杯", 50), ("p21578_cooker", "电饭煲", 400)):
        made = client.post(f"{B}/goods", headers=admin, json={"code": code, "name": name, "points": points, "stock": 5})
        assert made.status_code == 201, made.text
        goods[code] = made.json()["id"]
    return {"h": heads, "goods": goods}


def _draws(monkeypatch, values):
    """让出码的随机数按 `values` 依次给；多抽一次就当场报出来（抽了几次也是断言的一部分）。"""
    seq = iter(values)
    used: list[int] = []

    def fake(n):
        assert n == 1000000
        value = next(seq)
        used.append(value)
        return value

    monkeypatch.setattr(assess, "randbelow", fake)
    return used


def _redeem(client, headers, goods_id):
    return client.post(f"{B}/redeems", headers=headers, json={"goods_id": goods_id})


def _verify(client, headers, code):
    return client.post(f"{B}/redeems/verify", headers=headers, json={"verify_code": code})


def test_撞码重抽_第二单拿到不同的码_一个码只核得掉第一单(client, world, monkeypatch):
    h = world["h"]
    used = _draws(monkeypatch, [123456, 123456, 654321])
    first = _redeem(client, h["p21578_va"], world["goods"]["p21578_cup"])
    assert first.status_code == 201, first.text
    second = _redeem(client, h["p21578_vb"], world["goods"]["p21578_cooker"])
    assert second.status_code == 201, second.text
    assert first.json()["verify_code"] == "123456"
    assert second.json()["verify_code"] == "654321"   # 修前：也是 123456
    assert used == [123456, 123456, 654321]   # 第二单头一抽撞上第一单，重抽一次

    one = _verify(client, h["p21578_op"], "123456")
    assert one.status_code == 200, one.text
    assert one.json() == {"id": first.json()["id"], "status": "verified"}
    again = _verify(client, h["p21578_op"], "123456")
    assert again.status_code == 404, again.text   # 修前：200，核掉的是乙的电饭煲
    assert again.json()["detail"] == "核销码无效或已核销"
    other = _verify(client, h["p21578_op"], "654321")
    assert other.json() == {"id": second.json()["id"], "status": "verified"}


def test_已核销的码可以再发(client, world, monkeypatch):
    """只跟待核销的比：上一条里 123456 已核销，再抽到它照发。"""
    used = _draws(monkeypatch, [123456])
    made = _redeem(client, world["h"]["p21578_va"], world["goods"]["p21578_cup"])
    assert made.status_code == 201, made.text
    assert made.json()["verify_code"] == "123456" and used == [123456]


def test_连抽都撞上_409且库存与积分未扣(client, world, admin, monkeypatch):
    h = world["h"]
    _draws(monkeypatch, [777777])
    pending = _redeem(client, h["p21578_va"], world["goods"]["p21578_cup"])
    assert pending.status_code == 201, pending.text
    balance = client.get(f"{B}/point-accounts/me", headers=h["p21578_vb"]).json()["balance"]
    stock = {g["id"]: g["stock"] for g in client.get(f"{B}/goods", headers=admin).json()}

    used = _draws(monkeypatch, [777777] * assess.VERIFY_CODE_ATTEMPTS)
    blocked = _redeem(client, h["p21578_vb"], world["goods"]["p21578_cup"])
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == "核销码连续撞上未核销的兑换单，请重试（库存与积分未扣）"
    assert len(used) == assess.VERIFY_CODE_ATTEMPTS
    assert client.get(f"{B}/point-accounts/me", headers=h["p21578_vb"]).json()["balance"] == balance
    assert {g["id"]: g["stock"] for g in client.get(f"{B}/goods", headers=admin).json()} == stock


def test_存量里同码的待核销单_按兑换先后核(client, world):
    """修前存下的同码待核销单不改数据，核销按单号先后来。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount, SpdRedeem

    with SessionLocal() as db:
        accounts = [a.id for a in db.query(SpdPointAccount).order_by(SpdPointAccount.id).limit(2)]
        rows = [SpdRedeem(account_id=a, goods_id=world["goods"]["p21578_cup"], points=50, verify_code="246810",
                          status="pending") for a in accounts]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    op = world["h"]["p21578_op"]
    assert _verify(client, op, "246810").json() == {"id": ids[0], "status": "verified"}
    assert _verify(client, op, "246810").json() == {"id": ids[1], "status": "verified"}
    assert _verify(client, op, "246810").status_code == 404


def test_核销取单按单号排序():
    """SQLite 上同键的索引项本就按行号排，上一条修前也是绿的；PG 不排序取到哪张看堆里的物理顺序，所以把排序钉在源码上。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    func = next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "verify_redeem")
    picks = [ast.unparse(node) for node in ast.walk(func)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "first"]
    assert picks and all("SpdRedeem.verify_code" in p and ".order_by(SpdRedeem.id)" in p for p in picks), picks
