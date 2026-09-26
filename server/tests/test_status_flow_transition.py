"""六个「推进」状态机与同一状态机的核收 / 拒收 / 结案 / 响应申领：锁外读改写，交错时后提交的把先提交的盖回去（P2-317）。

消毒批次、急救事件、检验样本物流、缺药登记、病理标本、中药代煎配送六处的「推进」都是「读 status → 查流转表得下一态 →
赋值 → commit」，UPDATE 只有 `WHERE id = ?`：两路交错时，后提交的一路照自己读到的旧状态写——推进被退回一步；
缺药登记已被取消，后到的推进把它翻回「已配送」；病理标本核收与拒收交错，已拒收的被改成已核收；两人同时发放 /
响应申领 / 结案，库里只剩后写的那个接收机构、批次、结论，先写的那位拿到的 200 回执却是自己那份。

修法：`concurrency.move_row`——「状态还是我判过的那个」压进同一条 UPDATE，抢输的一路 409，库里保持先提交那一路的结果。
这里把「读到之后、写入之前，另一路先提交了」钉成确定的时序：推进在查流转表（`_FLOW.get(读到的状态)`）时插进另一路的
提交；核收 / 拒收 / 结案 / 响应申领在读到之后必经的归属校验（或取标本）里插进。
"""
import pytest

from app.database import SessionLocal


class _RacingFlow(dict):
    """流转表的替身：第一次 `get(读到的状态)` 时先让另一路提交——这一刻，这一路已经读到了旧状态、还没写。"""

    def __init__(self, flow, race):
        super().__init__(flow)
        self.race = race

    def get(self, key, default=None):
        if self.race is not None:
            race, self.race = self.race, None
            race()
        return super().get(key, default)


def _elsewhere(model, row_id, **values):
    """另一路（另一个会话）先把这一行改掉并提交。"""
    def race():
        with SessionLocal() as other:
            row = other.get(model, row_id)
            for key, value in values.items():
                setattr(row, key, value)
            other.commit()
    return race


def _row(model, row_id):
    with SessionLocal() as db:
        row = db.get(model, row_id)
        db.expunge(row)
        return row


def _after_check(module, name, race):
    """包一层「读到之后必经的函数」：真函数照跑，之后让另一路先提交。"""
    real = getattr(module, name)

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        wrapped.race, pending = None, wrapped.race
        if pending is not None:
            pending()
        return result

    wrapped.race = race
    return wrapped


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2317 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    other_org = client.post("/api/organizations", headers=admin, json={
        "name": "P2317 分院", "org_type": "village", "level": "village"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2317 患者", "id_card": "330106197001012317"}).json()["id"]
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
    return {"org": org, "other_org": other_org, "patient": patient, "operator": operator}


def _batch(client, admin, world, no):
    created = client.post("/api/cssd/batches", headers=admin, json={
        "org_id": world["org"], "center_org_id": world["org"], "batch_no": no, "item_name": "器械包", "quantity": 5})
    assert created.status_code == 201, created.text
    return created.json()["id"]


# ================================================================ 消毒供应
def test_消毒批次推进交错_不被退回一步(client, admin, world, monkeypatch):
    from app.models import SterilizationBatch
    from app.routers import cssd

    batch = _batch(client, admin, world, "P2317-B1")   # 灭菌中
    monkeypatch.setattr(cssd, "_FLOW", _RacingFlow(cssd._FLOW, _elsewhere(
        SterilizationBatch, batch, status="dispatched", dispatched_to_org_id=world["other_org"])))
    got = client.post(f"/api/cssd/batches/{batch}/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已发放被退回已灭菌
    assert got.json()["detail"] == "批次状态已变为 已发放，请刷新后再操作"
    assert _row(SterilizationBatch, batch).status == "dispatched"
    # 不并发时照常往下走
    back = client.post(f"/api/cssd/batches/{batch}/advance", headers=admin)
    assert back.status_code == 200 and back.json()["status"] == "recycled", back.text


def test_消毒批次两人同时发放_接收机构不被后写的盖掉(client, admin, world, monkeypatch):
    from app.models import SterilizationBatch
    from app.routers import cssd

    batch = _batch(client, admin, world, "P2317-B2")
    assert client.post(f"/api/cssd/batches/{batch}/advance", headers=admin).json()["status"] == "sterile"
    monkeypatch.setattr(cssd, "_FLOW", _RacingFlow(cssd._FLOW, _elsewhere(
        SterilizationBatch, batch, status="dispatched", dispatched_to_org_id=world["other_org"])))
    got = client.post(f"/api/cssd/batches/{batch}/advance", headers=admin,
                      params={"dispatched_to_org_id": world["org"]})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200、发给了本院
    row = _row(SterilizationBatch, batch)
    assert (row.status, row.dispatched_to_org_id) == ("dispatched", world["other_org"])


def test_两人同时响应同一申领_批次不被后写的盖掉(client, admin, world, monkeypatch):
    from app.models import CssdRequest
    from app.routers import cssd

    first, second = (_batch(client, admin, world, no) for no in ("P2317-B3", "P2317-B4"))
    for batch in (first, second):
        assert client.post(f"/api/cssd/batches/{batch}/advance", headers=admin).json()["status"] == "sterile"
    req = client.post("/api/cssd/requests", headers=admin,
                      json={"org_id": world["org"], "item_name": "换药包", "quantity": 2})
    assert req.status_code == 201, req.text
    req = req.json()["id"]
    monkeypatch.setattr(cssd, "assert_obj_org_writable", _after_check(
        cssd, "assert_obj_org_writable", _elsewhere(CssdRequest, req, status="fulfilled", batch_id=first)))
    got = client.post(f"/api/cssd/requests/{req}/fulfill", headers=admin, params={"batch_id": second})
    monkeypatch.undo()
    assert got.status_code == 409 and got.json()["detail"] == "申领已处理", got.text   # 修前 200、批次换成后一个
    assert _row(CssdRequest, req).batch_id == first


# ================================================================ 急救 / 检验样本 / 中药代煎
def test_急救事件推进交错_不被退回一步(client, admin, world, monkeypatch):
    from app.models import EmergencyCase
    from app.routers import emergency

    case = client.post("/api/emergency/cases", headers=admin, json={"location": "P2317 某路口"})
    assert case.status_code == 201, case.text
    case = case.json()["id"]   # 已调度
    monkeypatch.setattr(emergency, "_FLOW", _RacingFlow(emergency._FLOW, _elsewhere(EmergencyCase, case, status="arrived")))
    got = client.post(f"/api/emergency/cases/{case}/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已到院被退回转运中
    assert got.json()["detail"] == "事件状态已变为 已到院，请刷新后再操作"
    assert _row(EmergencyCase, case).status == "arrived"
    back = client.post(f"/api/emergency/cases/{case}/advance", headers=admin)
    assert back.status_code == 200 and back.json()["status"] == "admitted", back.text


def _exam(world, center_type, **extra):
    from app.models import ExamRequest

    with SessionLocal() as db:
        request = ExamRequest(patient_id=world["patient"], from_org_id=world["org"], center_type=center_type,
                              item_code="P2317", item_name="P2317 项目", status="pending",
                              created_by=world["operator"], **extra)
        db.add(request)
        db.commit()
        return request.id


def test_检验样本推进交错_不被退回一步_出报告后不再往前走(client, admin, world, monkeypatch):
    from app.models import ExamRequest
    from app.routers import exams

    request = _exam(world, "lab", sample_status="collected")
    monkeypatch.setattr(exams, "_SAMPLE_FLOW", _RacingFlow(exams._SAMPLE_FLOW, _elsewhere(
        ExamRequest, request, sample_status="received")))
    got = client.post(f"/api/exams/{request}/sample/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：中心核收被退回转运中
    assert got.json()["detail"] == "样本物流刚被他人推进，请刷新后再操作"
    assert _row(ExamRequest, request).sample_status == "received"

    reported = _exam(world, "lab", sample_status="collected")
    monkeypatch.setattr(exams, "_SAMPLE_FLOW", _RacingFlow(exams._SAMPLE_FLOW, _elsewhere(
        ExamRequest, reported, status="reported")))
    got = client.post(f"/api/exams/{reported}/sample/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409 and got.json()["detail"] == "申请单已出报告或已互认", got.text   # 修前 200
    assert _row(ExamRequest, reported).sample_status == "collected"


def test_中药订单推进交错_不被退回一步(client, admin, world, monkeypatch):
    from app.models import TcmDispenseOrder
    from app.routers import tcm

    order = client.post("/api/tcm/dispense-orders", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "herbs": "黄芪30g 白术15g", "doses": 7,
        "decoct": True})
    assert order.status_code == 201, order.text
    order = order.json()["id"]   # 已下单
    monkeypatch.setattr(tcm, "_DISPENSE_FLOW", _RacingFlow(tcm._DISPENSE_FLOW, _elsewhere(
        TcmDispenseOrder, order, status="decocted")))
    got = client.post(f"/api/tcm/dispense-orders/{order}/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已煎煮被退回已调配
    assert got.json()["detail"] == "订单状态已变为 已煎煮，请刷新后再操作"
    assert _row(TcmDispenseOrder, order).status == "decocted"


# ================================================================ 缺药登记
def _shortage(client, admin, world, code):
    created = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": "P2317 胰岛素", "quantity": 3})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_缺药登记推进与取消交错_不被翻回已配送(client, admin, world, monkeypatch):
    from app.models import DrugShortage
    from app.routers import medication

    shortage = _shortage(client, admin, world, "P2317A")
    assert client.post(f"/api/medication/shortages/{shortage}/advance", headers=admin).json()["status"] == "purchasing"
    monkeypatch.setattr(medication, "_SHORTAGE_FLOW", _RacingFlow(medication._SHORTAGE_FLOW, _elsewhere(
        DrugShortage, shortage, status="cancelled", close_reason="患者已转院")))
    got = client.post(f"/api/medication/shortages/{shortage}/advance", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已取消的被翻回已配送
    assert got.json()["detail"] == "登记状态已变为 已取消，请刷新后再操作"
    assert _row(DrugShortage, shortage).status == "cancelled"


def test_两人同时结案_结论不被后写的盖掉(client, admin, world, monkeypatch):
    from app.models import DrugShortage
    from app.routers import medication

    shortage = _shortage(client, admin, world, "P2317B")
    for _ in range(2):
        assert client.post(f"/api/medication/shortages/{shortage}/advance", headers=admin).status_code == 200
    monkeypatch.setattr(medication, "assert_obj_org_writable", _after_check(
        medication, "assert_obj_org_writable", _elsewhere(DrugShortage, shortage, status="collected")))
    got = client.post(f"/api/medication/shortages/{shortage}/close", headers=admin, json={"result": "no_show"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已取药被改成未取药，黑名单跟着多记一笔
    assert _row(DrugShortage, shortage).status == "collected"


# ================================================================ 病理标本
def _specimen(client, admin, world):
    request = _exam(world, "pathology")
    created = client.post("/api/pathology/specimens", headers=admin, json={"request_id": request})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_标本核收与拒收交错_后到的不盖掉先到的(client, admin, world, monkeypatch):
    from app.models import PathologySpecimen
    from app.routers import pathology

    rejected = _specimen(client, admin, world)
    monkeypatch.setattr(pathology, "_specimen", _after_check(pathology, "_specimen", _elsewhere(
        PathologySpecimen, rejected, status="rejected", reject_reason="标本量不足")))
    got = client.post(f"/api/pathology/specimens/{rejected}/receive", headers=admin, json={"received_by": "P2317 核收员"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已拒收的被改成已核收，拒收原因还挂着
    assert _row(PathologySpecimen, rejected).status == "rejected"

    received = _specimen(client, admin, world)
    monkeypatch.setattr(pathology, "_specimen", _after_check(pathology, "_specimen", _elsewhere(
        PathologySpecimen, received, status="received", received_by="P2317 先到的核收员")))
    got = client.post(f"/api/pathology/specimens/{received}/reject", headers=admin,
                      json={"reject_reason": "标本量不足"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已核收的被标成拒收
    assert _row(PathologySpecimen, received).status == "received"


def test_标本推进交错_不被退回一步(client, admin, world, monkeypatch):
    from app.models import PathologySpecimen
    from app.routers import pathology

    specimen = _specimen(client, admin, world)
    assert client.post(f"/api/pathology/specimens/{specimen}/receive", headers=admin,
                       json={"received_by": "P2317 核收员"}).status_code == 200
    monkeypatch.setattr(pathology, "SPECIMEN_FLOW", _RacingFlow(pathology.SPECIMEN_FLOW, _elsewhere(
        PathologySpecimen, specimen, status="slided", block_count=2, slide_count=6)))
    got = client.post(f"/api/pathology/specimens/{specimen}/advance", headers=admin, json={"block_count": 3})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已制片被退回已取材、蜡块数被改写
    assert got.json()["detail"] == "标本状态已变为 已制片，请刷新后再操作"
    row = _row(PathologySpecimen, specimen)
    assert (row.status, row.block_count) == ("slided", 2)
