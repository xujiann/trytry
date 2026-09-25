"""用血申请的审批 / 发血状态转换不是原子的：同一张申请并发点两次发血，库存扣两次（P2-110）。

`issue_blood` 原先是「内存里判 approved → 扣库存（条件 UPDATE）→ 改 issued」，`review_transfusion` 是
「内存里判 pending → 改 approved / rejected」。库存那一侧早是条件 UPDATE（两笔不同的申请并发发血不会超扣），状态这一侧
没有。PG 的 READ COMMITTED 下并发的两路都读到同一个旧状态、都往下走：

- 同一张申请连点两次「发血」：两路都判定已审批、各扣一次库存——发出去一袋，账上扣了两袋；
- 审批与驳回同时到：两路都成功，后提交的盖掉先提交的；锁外读到 pending 的那一路甚至能把已经发了血的申请改成驳回。

修法与预约（P2-109）、发药冲销、采购验收同一个写法：状态转换用条件 UPDATE（`WHERE status = 'approved'` /
`'pending'`），影响 0 行即回滚、409；发血先翻状态、再扣库存，库存不足时连同翻状态一并回滚。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）；
PG 上真并发的不变量见 test_blood_status_transition_races.py。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2110 血库医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patients = [
        client.post("/api/patients", headers=admin, json={
            "name": f"P2110 用血患者{i}", "id_card": f"33012719710{i}102110"}).json()["id"]
        for i in range(4)
    ]
    stock = client.post("/api/blood/stocks", headers=admin, json={
        "blood_type": "AB", "component": "plasma", "quantity_ml": 1000})
    assert stock.status_code == 200, stock.text
    return {"org": org, "patients": patients}


def _new_request(client, admin, world, patient_index, quantity_ml=200, approve=True):
    created = client.post("/api/blood/requests", headers=admin, json={
        "patient_id": world["patients"][patient_index], "org_id": world["org"],
        "blood_type": "AB", "component": "plasma", "quantity_ml": quantity_ml})
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    if approve:
        reviewed = client.post(f"/api/blood/requests/{request_id}/review?approve=true", headers=admin)
        assert reviewed.status_code == 200, reviewed.text
    return request_id


def _stock_ml():
    from app.database import SessionLocal
    from app.models import BloodStock

    with SessionLocal() as db:
        return db.query(BloodStock).filter_by(blood_type="AB", component="plasma").one().quantity_ml


def _status(request_id):
    from app.database import SessionLocal
    from app.models import TransfusionRequest

    with SessionLocal() as db:
        return db.get(TransfusionRequest, request_id).status


def _admin_user(db):
    from app.models import User

    return db.query(User).filter_by(username="admin").one()


def test_同一申请并发发血两次_库存只扣一次(client, admin, world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import TransfusionRequest
    from app.routers.blood import issue_blood

    request_id = _new_request(client, admin, world, 0)
    before = _stock_ml()
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(TransfusionRequest, request_id)   # noqa: F841 — 第一路先读到已审批；留着引用，身份映射是弱引用
        issue_blood(request_id, db=winner, user=_admin_user(winner))          # 第二路先发血成功
        with pytest.raises(HTTPException) as exc:
            issue_blood(request_id, db=racer, user=_admin_user(racer))        # 第一路拿着读到的已审批接着发
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "仅已审批申请可发血"   # 与顺序重复发血同一句
    assert _stock_ml() == before - 200   # 修前 -400：发出去一袋，账上扣了两袋
    assert _status(request_id) == "issued"


def test_审批与驳回同时到_只成一路(client, admin, world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import TransfusionRequest
    from app.routers.blood import review_transfusion

    request_id = _new_request(client, admin, world, 1, approve=False)
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(TransfusionRequest, request_id)   # noqa: F841 — 驳回那一路先读到待审批
        review_transfusion(request_id, approve=True, db=winner, user=_admin_user(winner))   # 审批那一路先提交
        with pytest.raises(HTTPException) as exc:
            review_transfusion(request_id, approve=False, db=racer, user=_admin_user(racer))
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "该申请已处理"
    assert _status(request_id) == "approved"   # 修前 rejected：审批人看到「已审批」，库里却是「已驳回」


def test_锁外读到待审批的驳回_不能把已发血的申请改成驳回(client, admin, world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import TransfusionRequest
    from app.routers.blood import review_transfusion

    request_id = _new_request(client, admin, world, 2, approve=False)
    with SessionLocal() as racer:
        held = racer.get(TransfusionRequest, request_id)   # noqa: F841 — 驳回那一路先读到待审批
        assert client.post(f"/api/blood/requests/{request_id}/review?approve=true", headers=admin).status_code == 200
        issued = client.post(f"/api/blood/requests/{request_id}/issue", headers=admin)
        assert issued.status_code == 200, issued.text   # 这期间另两路审批、发了血
        with pytest.raises(HTTPException) as exc:
            review_transfusion(request_id, approve=False, db=racer, user=_admin_user(racer))
    assert exc.value.status_code == 409   # 修前不报错
    assert _status(request_id) == "issued"   # 修前 rejected：血发出去了，申请却成了「已驳回」


def test_发血遇库存不足_申请照旧是已审批(client, admin, world):
    """先翻状态、再扣库存：扣不动要把翻成 issued 的那一步一并退掉，不能留下一张没扣库存的「已发血」。"""
    request_id = _new_request(client, admin, world, 3, quantity_ml=_stock_ml() + 100)
    before = _stock_ml()
    resp = client.post(f"/api/blood/requests/{request_id}/issue", headers=admin)
    assert resp.status_code == 409 and "血液库存不足" in resp.json()["detail"], resp.text
    assert _status(request_id) == "approved"
    assert _stock_ml() == before
