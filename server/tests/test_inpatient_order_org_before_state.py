"""开医嘱先判归属、再判「已出院」：别家医生按住院号探不出这次住院在不在院（P2-1699，第五十批扫描 AN4-7）。

修前：`create_order` 先判 `admission.status != "admitted"`（409「患者已出院，不可开立医嘱」），再 `assert_obj_org_writable`。实测乙院
医生拿甲院的住院号开医嘱：在院时 403、已出院时 409——按住院号顺序一遍，甲院每次住院在不在院都探得出来。同文件 `stop_order`
的注释原话「归属判定排在状态机之前：先 403，免得用 409/200 的差别探出别家医嘱的状态」，`test_cross_org_write_guards.py` 的
`test_归属判定排在状态机之前` 钉着停医嘱；执行、首页、转床、出院都先判归属。

修法：归属判定挪到状态判定之前（锁内那次复判不动），不新增守卫。本院对已出院的照旧 409。照抄停医嘱那一条的写法。
"""
import pytest
from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    doctors = {}
    for tag in ("a", "b"):
        orgs[tag] = client.post("/api/organizations", headers=admin, json={
            "name": f"P21699 {tag} 卫生院", "org_type": "township", "level": "township"}).json()["id"]
        created = client.post("/api/users", headers=admin, json={
            "username": f"p21699_doc_{tag}", "password": "passw0rd1", "role": "doctor", "org_id": orgs[tag]})
        assert created.status_code in (200, 201), created.text
        doctors[tag] = login(client, f"p21699_doc_{tag}", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": orgs["a"], "name": "P21699 内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21699-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21699 患者", "id_card": "330106196909091699", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=doctors["a"], json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return {"adm": adm.json()["id"], "doc_a": doctors["a"], "doc_b": doctors["b"]}


def _order(client, world, who):
    return client.post("/api/inpatient/orders", headers=world[who], json={
        "admission_id": world["adm"], "order_type": "temp", "content": "P21699 血常规"})


def test_别家医师对在院住院开医嘱_403(client, world):
    resp = _order(client, world, "doc_b")
    assert resp.status_code == 403, resp.text


def test_归属判定排在状态机之前(client, world):
    """住院此刻已出院。别家再来开医嘱，必须仍拿 403 而不是 409。

    否则 409 与 403 的差别就成了一个**旁路**：外人按住院号就能探出别家医院每次住院现在在不在院。
    """
    summary = client.post(f"/api/inpatient/admissions/{world['adm']}/case-summary", headers=world["doc_a"],
                          json={"discharge_diagnosis": "肺炎", "outcome": "好转"})
    assert summary.status_code == 201, summary.text
    discharged = client.post(f"/api/inpatient/admissions/{world['adm']}/discharge", headers=world["doc_a"])
    assert discharged.status_code == 200, discharged.text
    resp = _order(client, world, "doc_b")
    assert resp.status_code == 403, resp.text   # 修前 409「患者已出院，不可开立医嘱」


def test_本院对已出院住院开医嘱_照旧409(client, world):
    resp = _order(client, world, "doc_a")
    assert (resp.status_code, resp.json()["detail"]) == (409, "患者已出院，不可开立医嘱")
