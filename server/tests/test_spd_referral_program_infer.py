"""发起转诊不写病种，转诊单就不挂纳管档案：有效上转的积分记给录单的人、下转的承接随访任务不挂档案（P1-139）。

`POST /api/spd/referrals` 按「患者 + 病种」取纳管档案挂到转诊单上；之后接收时派的「到院跟踪」、到院时的「有效上转」
积分（给档案上的村医）、下转时派的「承接与随访」任务都取这份档案。管理端发起转诊的表单原先是个「病种编码」文本框，
不填就不挂档案——积分回落给录单的人（中心代录时是经办，不是这位患者的村医），下转的承接随访任务不挂档案，按档案看
不到它。与 P1-138（监测值没写病种一律判正常）、P2-100（上报不带病种）同一族。

修法：没写病种、患者只在管一个病种的，挂这份档案、病种取它的；在管几个病种的不替人猜，照旧不挂（界面上改成病种
下拉，可以选）。写了的照旧按写的。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1139 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    single = client.post("/api/patients", headers=admin, json={
        "name": "P1139 单病种", "id_card": "330127196705051139"}).json()["id"]
    multi = client.post("/api/patients", headers=admin, json={
        "name": "P1139 多病种", "id_card": "330127196706061139"}).json()["id"]
    enrollments = {}
    for patient, program in ((single, "hypertension"), (multi, "hypertension"), (multi, "diabetes")):
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org})
        assert resp.status_code == 201, resp.text
        enrollments[(patient, program)] = resp.json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p1139_doc", "password": "pw123456", "full_name": "P1139 医生", "role": "doctor", "org_id": org})
    assert doctor.status_code == 201, doctor.text
    from conftest import login

    return {"org": org, "single": single, "multi": multi, "enrollments": enrollments,
            "doctor": login(client, "p1139_doc", "pw123456")}


def _refer(client, admin, world, patient, **extra):
    """以本院医生发起（账号带机构：不挂档案也能定发起机构，照实际录单的人来）。"""
    resp = client.post(f"{B}/referrals", headers=world["doctor"], json={
        "patient_id": patient, "direction": "up", "target_org_id": world["org"], "reason": "P1139", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_只在管一个病种的_不写病种也挂上这份档案(client, admin, world):
    case = _refer(client, admin, world, world["single"])
    expected = world["enrollments"][(world["single"], "hypertension")]
    assert (case["program_code"], case["enrollment_id"]) == ("hypertension", expected), case   # 修前 ("", None)


def test_在管几个病种的不替人猜_写了的照旧按写的(client, admin, world):
    case = _refer(client, admin, world, world["multi"])
    assert (case["program_code"], case["enrollment_id"]) == ("", None), case
    chosen = _refer(client, admin, world, world["multi"], program_code="diabetes")
    assert chosen["enrollment_id"] == world["enrollments"][(world["multi"], "diabetes")], chosen
