"""消毒供应批次只能由所属中心建、流转、记成本（P0-35 第四批 / P1-74）。

灭菌批次在模型里写明了归属：`center_org_id`（哪家消毒供应中心的批次）。三个写端点却都不看它：

- 建批次：中心由请求体声明，端点只查这家机构存不存在；
- 流转（灭菌中 → 已灭菌 → 已发放 → 已回收）：按批次号直取，任何经办都能推——
  `SterilizationBatch` 没有名叫 `org_id` 的列，按 id 写的棘轮数不到它（P1-74）；
- 记成本：按批次号挂成本项，进中心的成本核算。

2026-09-24 实测：乙院经办以甲院名义建批次、把甲院的批次推到「已发放」、给甲院的批次记一笔成本，
三条全 200 / 201。修法照 `assert_org_writable` 的既定口径，归属取 `center_org_id`；
「发放给哪家」（`dispatched_to_org_id`）按设计就是别家，不在此列。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import CssdCostItem, SterilizationBatch


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


_seq = itertools.count(1)


@pytest.fixture(scope="module")
def cssd_world(client):
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "消毒中心甲院"), ("b", "消毒中心乙院"), ("c", "消毒接收丙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    for key in ("a", "b"):
        r = client.post("/api/users",
                        json={"username": f"p035d_op_{key}", "password": "pw123456", "full_name": f"p035d_op_{key}",
                              "role": "operator", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    heads = {k: _login(client, f"p035d_op_{k}") for k in ("a", "b")}

    def new_batch() -> int:
        r = client.post("/api/cssd/batches",
                        json={"batch_no": f"P035-{next(_seq)}", "center_org_id": orgs["a"], "item_name": "换药包",
                              "quantity": 10},
                        headers=heads["a"])
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return {"orgs": orgs, "h": heads, "new_batch": new_batch}


def _batch(batch_id: int) -> tuple:
    db = SessionLocal()
    try:
        b = db.get(SterilizationBatch, batch_id)
        return b.status, b.dispatched_to_org_id
    finally:
        db.close()


def _costs(batch_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(CssdCostItem).filter(CssdCostItem.batch_id == batch_id).count()
    finally:
        db.close()


def test_不得以别家中心名义建批次(client, cssd_world):
    r = client.post("/api/cssd/batches",
                    json={"batch_no": f"P035-X{next(_seq)}", "center_org_id": cssd_world["orgs"]["a"],
                          "item_name": "换药包", "quantity": 1},
                    headers=cssd_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert "机构名义" in r.json()["detail"], r.text


def test_别家经办推不动本中心的批次(client, cssd_world):
    bid = cssd_world["new_batch"]()
    r = client.post(f"/api/cssd/batches/{bid}/advance", headers=cssd_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert _batch(bid) == ("sterilizing", None), "被拒却推动了状态"


def test_先判归属再判状态(client, cssd_world):
    """已回收（终态）的批次，别家拿到的是 403 而不是 409——否则从状态码就读出了别家批次走到了哪一步。"""
    o, h = cssd_world["orgs"], cssd_world["h"]
    bid = cssd_world["new_batch"]()
    client.post(f"/api/cssd/batches/{bid}/advance", headers=h["a"])
    client.post(f"/api/cssd/batches/{bid}/advance?dispatched_to_org_id={o['c']}", headers=h["a"])
    client.post(f"/api/cssd/batches/{bid}/advance", headers=h["a"])
    assert _batch(bid)[0] == "recycled"
    assert client.post(f"/api/cssd/batches/{bid}/advance", headers=h["b"]).status_code == 403
    assert client.post(f"/api/cssd/batches/{bid}/advance", headers=h["a"]).status_code == 409


def test_本中心照常流转并发放给别家(client, cssd_world):
    o, h = cssd_world["orgs"], cssd_world["h"]
    bid = cssd_world["new_batch"]()
    assert client.post(f"/api/cssd/batches/{bid}/advance", headers=h["a"]).status_code == 200
    r = client.post(f"/api/cssd/batches/{bid}/advance?dispatched_to_org_id={o['c']}", headers=h["a"])
    assert r.status_code == 200, r.text
    assert _batch(bid) == ("dispatched", o["c"]), "发放给别家按设计照常"


def test_别家经办不得给本中心的批次记成本(client, cssd_world):
    bid = cssd_world["new_batch"]()
    body = {"batch_id": bid, "cost_type": "material", "amount": 88.5}
    r = client.post("/api/cssd/cost-items", json=body, headers=cssd_world["h"]["b"])
    assert r.status_code == 403, r.text
    assert _costs(bid) == 0
    assert client.post("/api/cssd/cost-items", json=body, headers=cssd_world["h"]["a"]).status_code == 201
    assert _costs(bid) == 1
