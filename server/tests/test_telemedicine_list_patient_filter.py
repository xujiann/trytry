"""在线咨询清单按患者筛（P2-1477，第四十三批「远程会诊与互联网诊疗」扫描 AG2-5）。

`GET /api/telemedicine/consults` 原先只收 `status`、回全县最新 200 条，`?patient_id=` 被静默忽略——与远程会诊清单
P2-1193 修前同一形状：甲卫生院答过「华法林不能和布洛芬同服，请改用对乙酰氨基酚」并结束的那条咨询，全县再来 200 条
之后就再也找不回（在线咨询没有 360 段、没有居民端入口）。修前实测（scan43 ag2/r4）：「按 patient_id=张三 查：返回
200 条；其中张三的 0 条」「status=closed 含张三那条: False」「页面清单含: False」。

修法照抄 P2-1193（`consultations.list_consultations`）：清单绑调用方身份，带患者号时先 `assert_patient_visible`
（资源名 online_consult，留痕）再过滤，与这位患者没有关系的机构 403；不带患者号照旧是全县最新 200 条——该按什么范围
给看随 P1-49 待裁定，一行也没收，也不切翻页（理由同 P2-1193）。

跨机构回复的一方与患者没有关系，按患者筛同样 403：与 P2-1193 同一口径，回复方的调阅依据随 AG2-2 待裁定，这一条不放宽。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog, OnlineConsult
from conftest import login

LIST = "/api/telemedicine/consults"


@pytest.fixture(scope="module")
def world(client, admin):
    """甲卫生院给患者甲建咨询、县医院跨机构回复、甲院结束；之后乙卫生院给患者乙来了 200 条；丙卫生院与两位患者都没有关系。"""
    orgs = {}
    for key, name, level in (("county", "P21477 县医院", "county"), ("a", "P21477 甲卫生院", "township"),
                             ("b", "P21477 乙卫生院", "township"), ("c", "P21477 丙卫生院", "township")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township" if level == "township" else "lead_hospital", "level": level})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key, role in (("a", "operator"), ("county", "doctor"), ("c", "doctor")):
        username = f"p21477_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patients = {}
    for key, id_card in (("a", "330106196001011477"), ("b", "330106196102021477")):
        resp = client.post("/api/patients", headers=admin, json={"name": f"P21477 患者{key}", "id_card": id_card})
        assert resp.status_code in (200, 201), resp.text
        patients[key] = resp.json()["id"]
    made = client.post(LIST, headers=heads["a"], json={
        "patient_id": patients["a"], "org_id": orgs["a"], "consult_type": "consult", "question": "华法林能和布洛芬一起吃吗"})
    assert made.status_code == 201, made.text
    target = made.json()["id"]
    assert client.post(f"{LIST}/{target}/reply", headers=heads["county"], json={
        "reply": "不能同服，布洛芬增加出血风险，请改用对乙酰氨基酚", "doctor_name": "县医院医生"}).status_code == 200
    assert client.post(f"{LIST}/{target}/close", headers=heads["a"]).status_code == 200
    with SessionLocal() as db:   # 之后全县又来了 200 条（乙卫生院给另一位患者的）
        db.add_all([OnlineConsult(patient_id=patients["b"], org_id=orgs["b"], consult_type="consult",
                                  question=f"P21477 咨询{i}", status="closed", reply="已答") for i in range(200)])
        db.commit()
    return {"orgs": orgs, "heads": heads, "patients": patients, "target": target}


def _logs(patient_id: int) -> int:
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.patient_id == patient_id,
                                          AccessLog.resource == "online_consult").count()


def test_再来200条之后_建单机构按患者筛得回这条已结束的咨询(client, world):
    latest = client.get(LIST, headers=world["heads"]["a"])
    assert latest.status_code == 200, latest.text
    assert len(latest.json()) == 200 and world["target"] not in {c["id"] for c in latest.json()}   # 修前就只有这一条路
    before = _logs(world["patients"]["a"])
    resp = client.get(LIST, headers=world["heads"]["a"], params={"patient_id": world["patients"]["a"]})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [c["id"] for c in rows] == [world["target"]]   # 修前参数被忽略，回的是全县最新 200 条、张三的 0 条
    assert (rows[0]["patient_id"], rows[0]["status"], rows[0]["reply"]) == (
        world["patients"]["a"], "closed", "不能同服，布洛芬增加出血风险，请改用对乙酰氨基酚")
    assert _logs(world["patients"]["a"]) == before + 1   # 按患者筛即调阅这位患者的咨询，留痕


def test_按患者筛与状态筛可以叠加(client, world):
    params = {"patient_id": world["patients"]["a"], "status": "closed"}
    assert [c["id"] for c in client.get(LIST, headers=world["heads"]["a"], params=params).json()] == [world["target"]]
    params["status"] = "open"
    assert client.get(LIST, headers=world["heads"]["a"], params=params).json() == []


def test_与患者无关的机构按患者筛_403_不留痕(client, world):
    before = _logs(world["patients"]["a"])
    resp = client.get(LIST, headers=world["heads"]["c"], params={"patient_id": world["patients"]["a"]})
    assert resp.status_code == 403, resp.text[:200]   # 修前参数被忽略、200
    # 跨机构回复的县医院同样与患者没有关系：与 P2-1193 同一口径，回复方的调阅依据随 AG2-2 待裁定，这一条不放宽
    resp = client.get(LIST, headers=world["heads"]["county"], params={"patient_id": world["patients"]["a"]})
    assert resp.status_code == 403, resp.text[:200]
    assert _logs(world["patients"]["a"]) == before


def test_不带患者号的清单字节不变_不看调用方_不留痕(client, admin, world):
    with SessionLocal() as db:
        newest = [cid for (cid,) in db.query(OnlineConsult.id).order_by(OnlineConsult.id.desc()).limit(200)]
        logs_before = db.query(AccessLog).count()
    for params in ({}, {"status": "closed"}):
        bodies = []
        for head in (admin, world["heads"]["a"], world["heads"]["c"]):   # 全域、建单机构、毫无关系的机构
            resp = client.get(LIST, headers=head, params=params)
            assert resp.status_code == 200, resp.text
            bodies.append(resp.content)
        assert bodies[0] == bodies[1] == bodies[2], params   # 补了调用方身份，不带患者号时口径一行没收
        assert [c["id"] for c in client.get(LIST, headers=admin, params=params).json()] == newest
    with SessionLocal() as db:
        assert db.query(AccessLog).count() == logs_before   # 不带患者定位的全表浏览不记（access_logs 的判准）
