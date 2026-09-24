"""待办铃铛里「待确认危急值」只列本机构申请单上的（P0-40）。

危急值报告一出，平台只通知**申请机构**的医生（`notify_staff(org_id=申请机构, roles=("doctor",))`，
处置留痕写的也是「已通知申请机构」）；确认接收按申请单的患者判可见性（P0-27）——别家的危急值
本就确认不了。可待办中心（`GET /api/todos`，网页右上角的铃铛与医生移动端的待办页都轮询它）
医生那一节按角色取数、不看机构：2026-09-24 实测，一家与谁都没有关系的新卫生院，医生的铃铛里
排着全县每一条未确认的危急值，连同**结论原文**（「血钾 6.8 mmol/L」这类检验结果）——每 30 秒刷一次。
与 P0-38 的「我的待办」同一个病（办不了的也算进待办），外加把别家患者的检验结论推到每个医生眼前。

修法：医生的「待确认危急值」按申请单的申请机构收到本机构（`visible_org_ids`；全域角色的
「未闭环危急值」一节照旧看全县）。「待诊断申请」一节同样不看机构，但谁能领取诊断取决于
「谁算共享中心」（待裁定清单 P1-71 问题 2），这里不动。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def critical_world(client):
    """甲卫生院给一位患者开了检验单，报告是危急值（未确认）；乙卫生院与这位患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "危急待办甲卫生院"), ("b", "危急待办乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p040_doc_{key}", "password": "pw123456", "full_name": f"p040_doc_{key}",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    h = {k: _login(client, f"p040_doc_{k}") for k in orgs}
    patient = client.post("/api/patients", json={"name": "危急待办患者", "id_card": "320000198807076655"},
                          headers=admin).json()
    req = client.post("/api/exams",
                      json={"patient_id": patient["id"], "from_org_id": orgs["a"], "center_type": "lab",
                            "item_code": "K", "item_name": "血钾"},
                      headers=h["a"])
    assert req.status_code == 201, req.text
    report = client.post(f"/api/exams/{req.json()['id']}/report",
                         json={"conclusion": "危急待办测试：血钾 6.8 mmol/L", "critical": True},
                         headers=h["a"])
    assert report.status_code == 201, report.text
    return {"admin": admin, "h": h, "report_id": report.json()["id"]}


def _section(client, headers, kind):
    r = client.get("/api/todos", headers=headers)
    assert r.status_code == 200, r.text
    return next((item for item in r.json()["items"] if item["type"] == kind), None)


def test_无关机构医生的铃铛里没有别家的危急值(client, critical_world):
    section = _section(client, critical_world["h"]["b"], "critical_ack")
    assert section is not None, "医生的待办里应当有「待确认危急值」这一节"
    assert critical_world["report_id"] not in {row["id"] for row in section["list"]}
    r = client.get("/api/todos", headers=critical_world["h"]["b"])
    assert "血钾 6.8" not in r.text


def test_前提_无关机构医生本来就确认不了别家的危急值(client, critical_world):
    r = client.post(f"/api/exams/reports/{critical_world['report_id']}/acknowledge",
                    headers=critical_world["h"]["b"])
    assert r.status_code == 403, r.text


def test_申请机构的医生照常收到待确认危急值(client, critical_world):
    section = _section(client, critical_world["h"]["a"], "critical_ack")
    assert critical_world["report_id"] in {row["id"] for row in section["list"]}
    assert section["count"] == len(section["list"]) >= 1


def test_全域角色的未闭环危急值照旧看全县(client, critical_world):
    section = _section(client, critical_world["admin"], "critical_report")
    assert critical_world["report_id"] in {row["id"] for row in section["list"]}
