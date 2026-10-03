"""远程会诊清单按患者筛（P2-1193，第三十四批「跨机构协作的两端」扫描 L2-6 清单那一半）。

`GET /api/consultations` 原先只收 `status`、回全县最新 200 条，`?patient_id=` 被静默忽略：县医院出具「建议停用美托洛尔，
尽快上转安装起搏器评估」之后全县又来了 200 张会诊，申请方会诊页里就再也找不回这张单子，按患者筛回的是全县最新一条。
修前实测（scan34 l2/r4）：「会诊页里还有这张: False | 清单行数 200」，`?patient_id=` 回的第一行是别的患者。

修法：清单收 `patient_id`，与兄弟清单同一句——先 `assert_patient_visible`（与本文件 `_get` 同一资源名 consultation，
留痕），再按患者过滤；与这位患者没有关系的机构 403。不带患者号照旧是全县最新 200 条、响应仍是数组，页面原有三处调用不变。

没做的：不带患者号时的翻页。这份清单没有按机构收口（申请方 / 受邀方 / 全县该谁看随 P1-49 待裁定，是 P2-8 剩余的 B 类），
切翻页就把它从「最多 200 行」放大成「整表可翻」再附全县总数，先不动。360 视图加会诊段、统一申请单带意见摘要另行裁定。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog, Consultation
from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    """甲卫生院申请、县医院受邀出意见；乙卫生院给另一位患者申请了 200 张；丙卫生院与两位患者都没有关系。"""
    orgs = {}
    for key, name, level in (("county", "P1193 县医院", "county"), ("a", "P1193 甲卫生院", "township"),
                             ("b", "P1193 乙卫生院", "township"), ("c", "P1193 丙卫生院", "township")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township" if level == "township" else "lead_hospital", "level": level})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key in orgs:
        username = f"p1193_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor",
            "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patients = {}
    for key, id_card in (("a", "330127196001011193"), ("b", "330127196102021194")):
        patients[key] = client.post("/api/patients", headers=admin, json={
            "name": f"P1193 患者{key}", "id_card": id_card}).json()["id"]
        visit = client.post("/api/encounters", headers=heads[key], json={
            "patient_id": patients[key], "org_id": orgs[key], "doctor_name": "镇医"})
        assert visit.status_code in (200, 201), visit.text
    made = client.post("/api/consultations", headers=heads["a"], json={
        "patient_id": patients["a"], "from_org_id": orgs["a"], "to_org_id": orgs["county"],
        "question": "反复晕厥，心电图示二度房室传导阻滞"})
    assert made.status_code == 201, made.text
    target = made.json()["id"]
    assert client.post(f"/api/consultations/{target}/accept", headers=heads["county"],
                       json={"expert_name": "孙主任"}).status_code == 200
    assert client.post(f"/api/consultations/{target}/complete", headers=heads["county"],
                       json={"opinion": "建议停用美托洛尔，尽快上转安装起搏器评估"}).status_code == 200
    with SessionLocal() as db:   # 之后全县又来了 200 张（乙卫生院给另一位患者申请的）
        db.add_all([Consultation(patient_id=patients["b"], from_org_id=orgs["b"], to_org_id=orgs["county"],
                                 question=f"P1193 会诊{i}", created_by=1) for i in range(200)])
        db.commit()
    return {"orgs": orgs, "heads": heads, "patients": patients, "target": target}


def _logs(patient_id: int) -> int:
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.patient_id == patient_id,
                                          AccessLog.resource == "consultation").count()


def test_按患者筛得回县医院出过意见的那张(client, world):
    latest = client.get("/api/consultations", headers=world["heads"]["a"])
    assert latest.status_code == 200, latest.text
    assert len(latest.json()) == 200 and world["target"] not in {c["id"] for c in latest.json()}   # 修前就只有这一条路
    before = _logs(world["patients"]["a"])
    for who in ("a", "county"):   # 申请方与受邀方都看得这位患者
        resp = client.get("/api/consultations", headers=world["heads"][who],
                          params={"patient_id": world["patients"]["a"]})
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert [c["id"] for c in rows] == [world["target"]], who   # 修前参数被忽略，回的是全县最新 200 条
        assert (rows[0]["patient_id"], rows[0]["status"], rows[0]["opinion"]) == (
            world["patients"]["a"], "completed", "建议停用美托洛尔，尽快上转安装起搏器评估")
    assert _logs(world["patients"]["a"]) == before + 2   # 按患者筛即调阅这位患者的会诊，逐次留痕


def test_按患者筛与状态筛可以叠加(client, world):
    params = {"patient_id": world["patients"]["b"], "status": "applied"}
    rows = client.get("/api/consultations", headers=world["heads"]["b"], params=params).json()
    assert len(rows) == 200 and {c["patient_id"] for c in rows} == {world["patients"]["b"]}
    params["status"] = "completed"
    assert client.get("/api/consultations", headers=world["heads"]["b"], params=params).json() == []


def test_与患者没有关系的机构按患者筛_403(client, world):
    before = _logs(world["patients"]["a"])
    resp = client.get("/api/consultations", headers=world["heads"]["c"],
                      params={"patient_id": world["patients"]["a"]})
    assert resp.status_code == 403, resp.text[:200]   # 修前参数被忽略、200
    assert _logs(world["patients"]["a"]) == before


def test_不带患者号照旧是全县最新200条(client, world):
    for params in ({}, {"status": "applied"}):
        resp = client.get("/api/consultations", headers=world["heads"]["c"], params=params)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert isinstance(rows, list) and len(rows) == 200
        assert [c["id"] for c in rows] == sorted((c["id"] for c in rows), reverse=True)
