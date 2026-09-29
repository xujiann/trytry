"""HL7 A01 按病区名找病区只在调用方能写入的机构里找（P2-727，第十九批「导入 / 入站对接 vs 界面录入」扫描 K3-6）。

原先全县按名找：两家医院都有「内科病区」（县域里再常见不过）时，本院对接账号的每一条 A01 都 422「病区名在多家机构存在，
无法定位床位」——而这位账号本来只能收进本院的病区（入院登记按病区机构判写权）。同一个入院走界面按病区 id 办，照常。
修法：与 `assert_org_writable` 同一判据，非全域角色只在本机构的病区里按名找；全域角色照旧全县找、同名仍 422。
"""
import pytest

WARD = "P2727 内科病区"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, level, org_type in (("甲", "county", "lead_hospital"), ("乙", "township", "township")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2727 {key}院", "org_type": org_type, "level": level}).json()["id"]
        ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": orgs[key], "name": WARD}).json()
        for bed_no in ("01", "02"):
            assert client.post("/api/inpatient/beds", headers=admin,
                               json={"ward_id": ward["id"], "bed_no": bed_no}).status_code == 201
    created = client.post("/api/users", headers=admin, json={
        "username": "p2727_his", "password": "passw0rd1", "full_name": "p2727_his", "role": "operator",
        "org_id": orgs["甲"]})
    assert created.status_code in (200, 201), created.text
    return {"orgs": orgs}


def _a01(client, headers, id_card, bed_no, control_id):
    msg = "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260929100000||ADT^A01|{control_id}|P|2.4",
                     f"PID|1||{id_card}^^^CN^ID||P2727 住院{bed_no}||19650101|M",
                     f"PV1|1|I|{WARD}^^{bed_no}||||D001^李^医生"])
    return client.post("/api/integration/hl7v2/adt", headers=headers, json={"message": msg})


def test_本院对接账号按名找到本院病区_不因别家同名422(client, world):
    resp = _a01(client, _login(client, "p2727_his"), "330102196501012727", "01", "P2727A")
    assert resp.status_code == 201, resp.text   # 修前 422「病区名 … 在多家机构存在，无法定位床位」
    admissions = client.get(f"/api/inpatient/admissions?patient_id={resp.json()['patient']['id']}",
                            headers=_login(client, "p2727_his")).json()
    assert [a["org_id"] for a in admissions] == [world["orgs"]["甲"]]


def test_全域角色照旧全县找_同名仍422(client, admin, world):
    resp = _a01(client, admin, "330102196501022727", "02", "P2727B")
    assert resp.status_code == 422 and "多家机构" in resp.json()["detail"], resp.text
