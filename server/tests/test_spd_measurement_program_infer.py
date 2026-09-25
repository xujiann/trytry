"""监测值没写病种一律判「正常」：高血压在管患者收缩压 190，不判异常、不派处置任务（P1-138）。

判级按「病种 + 指标」取管理目标（`service.target_for`）。病种没写就是空串，空串没有管理目标可比，`judge_measurement`
一律回 normal——而管理端「监测数据录入」表单的病种下拉默认「全部病种」（空），居民端自报是个「病种编码（可留空）」的
文本框（居民不认得 hypertension 这种编码）。没人写的时候，「按管理目标即时判级，偏高 / 偏低自动生成处置任务」一条都
不发生，异常清单里也没有它。

修法：没写病种的，取这位患者在管档案里给这个指标配了管理目标的病种（按建档先后取第一个）判级、记在这个病种下；都
没有才留空（判「正常」，与原先一样）。写了的照旧按写的。单条录入、设备批量、居民自报三处同一口径。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1138 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    enrolled = client.post("/api/patients", headers=admin, json={
        "name": "P1138 在管", "id_card": "330127196603031138", "phone": "13911381138"}).json()
    loner = client.post("/api/patients", headers=admin, json={
        "name": "P1138 未纳管", "id_card": "330127196604041138"}).json()["id"]
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": enrolled["id"], "program_code": "hypertension", "org_id": org})
    assert resp.status_code == 201, resp.text
    return {"enrolled": enrolled, "loner": loner, "enrollment": resp.json()["id"]}


def _disposal_tasks(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter(SpdTask.patient_id == patient_id,
                                        SpdTask.title.like("指标异常处置%")).count()


def test_管理端录入不写病种_按在管档案判级_异常派处置任务(client, admin, world):
    pid = world["enrolled"]["id"]
    resp = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": pid, "metric": "bp_sys", "value": 190, "unit": "mmHg"})
    assert resp.status_code == 201, resp.text
    assert (resp.json()["level"], resp.json()["program_code"]) == ("high", "hypertension")   # 修前 ("normal", "")
    assert _disposal_tasks(pid) == 1   # 修前 0


def test_设备批量不写病种_同一口径(client, admin, world):
    resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
        {"patient_id": world["enrolled"]["id"], "metric": "bp_dia", "value": 115, "source": "device"}]})
    assert resp.status_code == 200 and resp.json() == {"created": 1, "abnormal": 1}, resp.text   # 修前 abnormal 0


def test_写了病种的照旧按写的_没有在管档案的照旧判正常(client, admin, world):
    kept = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": world["enrolled"]["id"], "metric": "bp_sys", "value": 190, "program_code": "diabetes"})
    assert kept.status_code == 201 and kept.json()["program_code"] == "diabetes", kept.text
    assert kept.json()["level"] == "normal"   # 糖尿病没给收缩压配目标：写了什么就按什么，不替人改
    loner = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": world["loner"], "metric": "bp_sys", "value": 190})
    assert loner.status_code == 201 and (loner.json()["level"], loner.json()["program_code"]) == ("normal", "")


def test_居民自报不写病种_按在管档案判级(client, world):
    phone = world["enrolled"]["phone"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text
    ph = {"Authorization": f"Bearer {login.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=ph, json={
        "name": world["enrolled"]["name"], "id_card": world["enrolled"]["id_card"]})
    assert bound.status_code == 200 or bound.json() == {"detail": "该账户已完成实名绑定"}, bound.text
    resp = client.post("/api/portal/spd/measurements", headers=ph, json={"metric": "bp_sys", "value": 185})
    assert resp.status_code == 201, resp.text
    assert resp.json()["level"] == "high"   # 修前 normal
