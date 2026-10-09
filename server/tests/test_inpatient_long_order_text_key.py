"""「执行中的长期医嘱同内容只许一条」按比对键查重：多一个空格、全角空格、大小写不同的同一条长期医嘱拿 409（P2-1697，
第五十批扫描 AN4-5）。

修前：`create_order` 的应用层查重按字面（`InpatientOrder.content == body.content`），库内部分唯一索引同样按原文；页面
`spdModal` 只去首尾空白，中间的空格、全角空格照收。实测「头孢呋辛 1.5g ivgtt bid」开立后，「头孢呋辛␣␣1.5g…」「头孢呋辛　1.5g…」
「…BID」三条都 201，同一次住院 4 条执行中的同一长期医嘱——注释自己写着「两条就是两行医嘱单、两笔执行登记，最后要主管医师回头
人工仲裁停掉一条」。

修法：应用层查重改按 `texttypes.text_key`（NFKC、去全部空白、不分大小写，DRG / 审方 / 随访匹配早已统一用它）在这次住院执行中的
长期医嘱里比，撞上报同一句 409；库内部分唯一索引不动（只兜字面完全相同的并发 / 双击）。存量不动。临时医嘱、别的住院、
停止后重开照旧。
"""
import pytest
from conftest import login

from app.database import SessionLocal

ORIGINAL = "P21697 头孢呋辛 1.5g ivgtt bid"
VARIANTS = ["P21697 头孢呋辛  1.5g ivgtt bid", "P21697 头孢呋辛　1.5g ivgtt bid", "P21697 头孢呋辛 1.5g ivgtt BID"]
DETAIL = "该住院已有内容相同的执行中长期医嘱，请先停止原医嘱再开立"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21697 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21697_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    doctor = login(client, "p21697_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21697 内科"}).json()["id"]
    adms = []
    for i in range(2):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P21697-{i}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21697 患者{i}", "id_card": f"33010619680808{i:04d}", "gender": "男"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=doctor, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
        assert adm.status_code == 201, adm.text
        adms.append(adm.json()["id"])
    return {"adms": adms, "doctor": doctor}


def _open(client, world, content, order_type="long", adm=0):
    return client.post("/api/inpatient/orders", headers=world["doctor"], json={
        "admission_id": world["adms"][adm], "order_type": order_type, "content": content})


def _active_long(adm_id: int) -> list[str]:
    from app.models import InpatientOrder

    with SessionLocal() as db:
        return [c for (c,) in db.query(InpatientOrder.content).filter(
            InpatientOrder.admission_id == adm_id, InpatientOrder.order_type == "long",
            InpatientOrder.status == "active").order_by(InpatientOrder.id)]


def test_空格_全角空格_大小写不同的同一条长期医嘱_409不落库(client, world):
    first = _open(client, world, ORIGINAL)
    assert first.status_code == 201, first.text
    for variant in VARIANTS:
        resp = _open(client, world, variant)
        assert resp.status_code == 409, (variant, resp.text)   # 修前三条都 201
        assert resp.json()["detail"] == DETAIL   # 与字面完全相同、并发抢输同一句
    assert _active_long(world["adms"][0]) == [ORIGINAL]   # 修前 4 条


def test_临时医嘱不受影响(client, world):
    for content in [ORIGINAL, *VARIANTS]:
        resp = _open(client, world, content, order_type="temp")
        assert resp.status_code == 201, (content, resp.text)


def test_别的住院照收(client, world):
    resp = _open(client, world, VARIANTS[2], adm=1)
    assert resp.status_code == 201, resp.text


def test_停止原医嘱后换个写法重开照收(client, world):
    (original,) = [o for o in client.get(f"/api/inpatient/orders?admission_id={world['adms'][0]}&status=active",
                                         headers=world["doctor"]).json()
                   if o["order_type"] == "long"]
    assert client.post(f"/api/inpatient/orders/{original['id']}/stop", headers=world["doctor"]).status_code == 200
    resp = _open(client, world, VARIANTS[0])
    assert resp.status_code == 201, resp.text
    assert _active_long(world["adms"][0]) == [VARIANTS[0]]
