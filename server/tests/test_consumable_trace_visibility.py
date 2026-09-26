"""高值耗材按条码正向追溯不收调用方（P0-46）。

`GET /api/materials/consumables/trace/{barcode}` 回这枚耗材植入了谁（`used_patient_name`）、哪台手术——原先连 `user` 形参
都没有：任一登录账号在物资页的追溯框里填别家的条码就读得到（修前实测 200），也不留痕。同文件的清单（反向追溯）早就收了：
按患者反查走患者可见性并留痕，按批号反查「同样只限本机构」。原先登记在读侧欠账名单里
（`test_stage15_horizontal.NEWLY_VISIBLE_UNGUARDED_READS`），兄弟端点的口径现成，没有口径问题。

修法：与清单同一句——本机构的照看；别家的，用在了某位患者身上就按那位患者的可见性判并留痕，没用过的不给看。
"""
import pytest

from app.database import SessionLocal

B = "/api/materials/consumables"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Encounter

    a = client.post("/api/organizations", headers=admin, json={
        "name": "P0046 介入中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    b = client.post("/api/organizations", headers=admin, json={
        "name": "P0046 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    c = client.post("/api/organizations", headers=admin, json={
        "name": "P0046 随访卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, org, role in (("p0046_op_a", a, "operator"), ("p0046_doc_b", b, "doctor"),
                                ("p0046_doc_c", c, "doctor")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": role, "org_id": org})
        assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P0046 患者", "id_card": "330127197309090046"}).json()["id"]
    for barcode in ("P0046-STENT-USED", "P0046-STENT-STOCK"):
        item = client.post(B, headers=admin, json={
            "barcode": barcode, "name": "冠脉支架", "org_id": a, "expire_date": "2099-12-31"})
        assert item.status_code == 201, item.text
    used = client.post(f"{B}/P0046-STENT-USED/use", headers=admin, json={"patient_id": patient})
    assert used.status_code == 200, used.text
    with SessionLocal() as db:   # 随访卫生院接诊过这位患者：与患者有服务关系
        db.add(Encounter(patient_id=patient, org_id=c, encounter_type="outpatient"))
        db.commit()
    return {"patient": patient, "op_a": _login(client, "p0046_op_a"),
            "doc_b": _login(client, "p0046_doc_b"), "doc_c": _login(client, "p0046_doc_c")}


def _access_rows(patient_id):
    from app.models import AccessLog

    with SessionLocal() as db:
        return db.query(AccessLog).filter_by(patient_id=patient_id, resource="consumable").count()


def test_无关机构填别家条码读不到植入了谁_403(client, world):
    got = client.get(f"{B}/trace/P0046-STENT-USED", headers=world["doc_b"])
    assert got.status_code == 403, got.text   # 修前 200：used_patient_name 就是患者姓名
    assert "P0046 患者" not in got.text


def test_别家没用过的耗材不给看_与按批号反查只限本机构同一句(client, world):
    assert client.get(f"{B}/trace/P0046-STENT-STOCK", headers=world["doc_b"]).status_code == 403   # 修前 200


def test_与患者有服务关系的别家机构看得到且留痕(client, world):
    before = _access_rows(world["patient"])
    got = client.get(f"{B}/trace/P0046-STENT-USED", headers=world["doc_c"])
    assert got.status_code == 200, got.text
    assert got.json()["used_patient_name"] == "P0046 患者"
    assert _access_rows(world["patient"]) == before + 1


def test_本机构照常追溯(client, world):
    for barcode in ("P0046-STENT-USED", "P0046-STENT-STOCK"):
        got = client.get(f"{B}/trace/{barcode}", headers=world["op_a"])
        assert got.status_code == 200, (barcode, got.text)


def test_不存在的条码仍是404(client, world):
    assert client.get(f"{B}/trace/P0046-NOPE", headers=world["doc_b"]).status_code == 404
