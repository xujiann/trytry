"""入库与召回并发：读到「批次正常」之后别人刚召回，这一笔照样加进已召回的批次与汇总——幽灵库存（P2-350）。

「召回后不得再入库」（`recall_batch`、P1-147）：按批次入库、调入、兜底批次三处都先判 `batch.status != "normal"` 再
`add_amount` 无条件累加。判是锁外读的：读到「正常」之后召回提交，这一笔照样加进已召回的批次、汇总跟着涨——
一片也发不出（发药只取正常批次），缺药预警与采购建议却当有货。

修法：累加时带上「还没召回」（`_add_to_normal_batch`，同一条 UPDATE），加不上即 409。这里把「读到的是召回前的
状态」钉成确定的时序：批次召回之后，让处理函数手上那份对象仍是「正常」（不写库，只改会话里读到的值）。
"""
import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.database import SessionLocal

B = "/api/pharmacy"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2350 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("甲院", "乙院")]
    return {"a": orgs[0], "b": orgs[1], "n": 0}


def _receive(client, admin, org, drug, batch_no, quantity=10):
    return client.post(f"{B}/batches", headers=admin, json={
        "org_id": org, "drug_code": drug, "drug_name": f"{drug} 片", "batch_no": batch_no,
        "expire_date": "2027-12-31", "quantity": quantity})


def _recall(client, admin, batch_id):
    got = client.post(f"{B}/batches/{batch_id}/recall", headers=admin, json={"reason": "P2350 召回"})
    assert got.status_code == 200, got.text


def _stale_normal(monkeypatch, batch_ids):
    """处理函数取到这些批次时，手上的对象仍是召回前的「正常」（模拟召回在判定之后才提交）。"""
    from app.models import DrugBatch
    from app.routers import pharmacy

    real, fired = pharmacy.ensure_present, []

    def stale(obj, *args, **kwargs):
        result = real(obj, *args, **kwargs)
        if isinstance(result, DrugBatch) and result.id in batch_ids:
            set_committed_value(result, "status", "normal")
            fired.append(result.id)
        return result

    monkeypatch.setattr(pharmacy, "ensure_present", stale)
    return fired


def _batch(batch_id):
    from app.models import DrugBatch

    with SessionLocal() as db:
        row = db.get(DrugBatch, batch_id)
        return row.status, row.quantity


def _stock(org, drug):
    from app.models import DrugStock

    with SessionLocal() as db:
        row = db.query(DrugStock).filter(DrugStock.org_id == org, DrugStock.drug_code == drug).first()
        return row.quantity if row else None


def test_按批次入库与召回并发_不加进已召回的批次(client, admin, world, monkeypatch):
    first = _receive(client, admin, world["a"], "P2350A", "LOT-A")
    assert first.status_code == 201, first.text
    batch = first.json()["id"]
    _recall(client, admin, batch)
    before = (_batch(batch), _stock(world["a"], "P2350A"))
    fired = _stale_normal(monkeypatch, {batch})
    resp = _receive(client, admin, world["a"], "P2350A", "LOT-A")
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201：加进了已召回的批次
    assert resp.json() == {"detail": "该批次已召回，不得再入库"}
    assert (_batch(batch), _stock(world["a"], "P2350A")) == before


def test_调入与调入方批次召回并发_不加进已召回的批次(client, admin, world, monkeypatch):
    source = _receive(client, admin, world["a"], "P2350T", "LOT-T", quantity=20)
    target = _receive(client, admin, world["b"], "P2350T", "LOT-T", quantity=5)
    assert source.status_code == target.status_code == 201, (source.text, target.text)
    _recall(client, admin, target.json()["id"])
    before = (_batch(target.json()["id"]), _stock(world["b"], "P2350T"), _stock(world["a"], "P2350T"))
    fired = _stale_normal(monkeypatch, {target.json()["id"]})
    resp = client.post(f"{B}/transfers", headers=admin, json={
        "drug_code": "P2350T", "from_org_id": world["a"], "to_org_id": world["b"], "quantity": 4})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json() == {"detail": "调入机构批号 LOT-T 已召回，不得调入"}
    assert (_batch(target.json()["id"]), _stock(world["b"], "P2350T"), _stock(world["a"], "P2350T")) == before


def test_直接入库与兜底批次召回并发_不加进已召回的兜底批次(client, admin, world, monkeypatch):
    from app.models import DrugBatch
    from app.routers.pharmacy import UNSPECIFIED_BATCH_NO

    first = client.post(f"{B}/stocks", headers=admin, json={
        "org_id": world["a"], "drug_code": "P2350U", "drug_name": "P2350U 片", "quantity": 10})
    assert first.status_code == 200, first.text
    with SessionLocal() as db:
        fallback = db.query(DrugBatch).filter(DrugBatch.org_id == world["a"], DrugBatch.drug_code == "P2350U",
                                              DrugBatch.batch_no == UNSPECIFIED_BATCH_NO).one().id
    _recall(client, admin, fallback)
    before = (_batch(fallback), _stock(world["a"], "P2350U"))
    fired = _stale_normal(monkeypatch, {fallback})
    resp = client.post(f"{B}/stocks", headers=admin, json={
        "org_id": world["a"], "drug_code": "P2350U", "drug_name": "P2350U 片", "quantity": 7})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert (_batch(fallback), _stock(world["a"], "P2350U")) == before


def test_没有召回时照常累加(client, admin, world):
    first = _receive(client, admin, world["a"], "P2350N", "LOT-N", quantity=3)
    again = _receive(client, admin, world["a"], "P2350N", "LOT-N", quantity=4)
    assert first.status_code == again.status_code == 201, again.text
    assert _batch(first.json()["id"]) == ("normal", 7)
    assert _stock(world["a"], "P2350N") == 7
