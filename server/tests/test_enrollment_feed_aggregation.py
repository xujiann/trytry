"""ADR-0003 方案 B 第二个概念：居民端**入组档案**读侧聚合。

与转诊同一个毛病：同一件事——"这个人被纳入了哪些疾病管理"——居民端有两份。
`/me/archive` 的 `chronic_care` 读平台 `chronic_patients`，
`/spd/archive` 的 `enrollments` 读 `spd_enrollments`，看到哪份取决于点了哪个入口。

本文件分两半：先钉住两个老接口**当前**的形状（聚合是叠加的只读层，老接口
一个字节都不该动），再验聚合行为。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import login, reset_database

from app.database import SessionLocal
from app.main import app
from app.models import ChronicPatient, Organization, Patient, ResidentAccount


@pytest.fixture()
def client():
    """函数级：断言涉及全量列表内容，共享库会引入顺序耦合。

    同时清掉居民端发码限流——它存在进程内状态里，`reset_database()` 冲不掉。
    """
    from app.routers.portal import _reset_portal_failures

    reset_database()
    _reset_portal_failures()
    with TestClient(app) as c:
        yield c


def _admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture()
def resident(client):
    admin = _admin(client)
    org = client.post(
        "/api/organizations",
        json={"name": "入组聚合卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "入组居民", "id_card": "330281199304045526", "phone": "13800002222"},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        # 平台慢病档案：高血压、2 级（需干预）
        db.add(ChronicPatient(
            patient_id=patient["id"], disease="hypertension",
            level=2, managed_by_org_id=org["id"], next_due="2026-09-01",
        ))
        db.add(ResidentAccount(
            phone="13800002222", patient_id=patient["id"], status="active",
        ))
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13800002222"}
    ).json()["debug_code"]
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800002222", "code": code}
    )
    assert token.status_code == 200, token.text
    return {
        "headers": {"Authorization": f"Bearer {token.json()['access_token']}"},
        "patient": patient, "org": org,
    }


def _add_spd_enrollment(patient_id, org_id, **kw):
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        db.add(SpdEnrollment(
            patient_id=patient_id, org_id=org_id,
            program_code=kw.get("program_code", "diabetes"),
            risk_level=kw.get("risk_level", "high"),
            stage=kw.get("stage", "管理期"),
            status=kw.get("status", "active"),
        ))
        db.commit()


# ---------------------------------------------------------------- 特征化网


def test_特征化_平台档案的chronic_care段形状不变(client, resident):
    archive = client.get("/api/portal/me/archive", headers=resident["headers"])
    assert archive.status_code == 200, archive.text
    care = archive.json()["chronic_care"]
    assert len(care) == 1
    assert set(care[0]) == {"disease", "level", "next_followup_due", "guidance_points"}
    assert care[0]["disease"] == "hypertension"
    assert care[0]["level"] == 2          # 平台原始码，无标签


def test_特征化_慢专病档案的profiles段形状不变(client, resident):
    _add_spd_enrollment(resident["patient"]["id"], resident["org"]["id"])
    archive = client.get("/api/portal/spd/archive", headers=resident["headers"])
    assert archive.status_code == 200, archive.text
    rows = archive.json()["profiles"]
    assert len(rows) == 1
    assert set(rows[0]) == {
        "program_code", "program_name", "status", "stage", "risk_level",
        "habits", "risk_factors", "complications", "tags",
    }
    assert rows[0]["program_code"] == "diabetes"
    assert rows[0]["risk_level"] == "high"   # spd 原始码，无标签


# ---------------------------------------------------------------- 聚合行为


def test_两个源真的并到了一起(client, resident):
    _add_spd_enrollment(resident["patient"]["id"], resident["org"]["id"])
    items = client.get("/api/portal/me/enrollments/all",
                       headers=resident["headers"]).json()
    assert {i["source"] for i in items} == {"platform", "spd"}, \
        f"两个源都要在，实际 {[i['source'] for i in items]}"

    platform = next(i for i in items if i["source"] == "platform")
    spd = next(i for i in items if i["source"] == "spd")
    assert platform["program_name"] == "高血压", "病种名要从目录取，不是回显编码"
    assert platform["status_label"] == "在管"
    assert platform["next_followup_due"] == "2026-09-01"
    assert platform["org"] == "入组聚合卫生院"
    assert spd["program_name"] == "2型糖尿病"
    assert spd["stage"] == "管理期"


def test_两套分级不互相映射_各留原始码与自己的标签(client, resident):
    """平台 level 是 1/2/3（控制良好/需干预/高危），spd 是 low/mid/high/very_high。

    两把尺子量的不是同一件事——一个说当前控制情况，一个说并发症风险。
    硬映射成一套码就是在编一个并不存在的等价关系（与转诊那边"同名不同义"同理）。
    """
    _add_spd_enrollment(resident["patient"]["id"], resident["org"]["id"], risk_level="high")
    items = client.get("/api/portal/me/enrollments/all",
                       headers=resident["headers"]).json()
    platform = next(i for i in items if i["source"] == "platform")
    spd = next(i for i in items if i["source"] == "spd")

    assert platform["level_code"] == "2" and platform["level_label"] == "需干预"
    assert spd["level_code"] == "high" and spd["level_label"] == "高危"
    # 两边都叫"高"的那档也不该被折算到一起：平台 3 才是"高危"，而 spd 的 high
    # 与平台的 3 并非同义。这里只断言"没有被映射成同一套码"。
    assert platform["level_code"] != spd["level_code"]


def test_字段集合就是契约声明的那些(client, resident):
    _add_spd_enrollment(resident["patient"]["id"], resident["org"]["id"])
    item = client.get("/api/portal/me/enrollments/all",
                      headers=resident["headers"]).json()[0]
    assert set(item) == {
        "source", "id", "program_code", "program_name", "status", "status_label",
        "level_code", "level_label", "stage", "org", "next_followup_due",
        "created_at", "date",
    }
    assert item["date"] == item["created_at"][:10]


def test_source参数在服务端收窄(client, resident):
    _add_spd_enrollment(resident["patient"]["id"], resident["org"]["id"])
    only_spd = client.get("/api/portal/me/enrollments/all?source=spd",
                          headers=resident["headers"]).json()
    assert {i["source"] for i in only_spd} == {"spd"}
    only_platform = client.get("/api/portal/me/enrollments/all?source=platform",
                               headers=resident["headers"]).json()
    assert {i["source"] for i in only_platform} == {"platform"}


def test_未知source拒绝而不是静默返回空(client, resident):
    resp = client.get("/api/portal/me/enrollments/all?source=nope",
                      headers=resident["headers"])
    assert resp.status_code == 422, resp.text


def test_子系统未装载时降级为平台单源(client, resident):
    from app.routers import portal as portal_mod

    kept = dict(portal_mod._ENROLLMENT_SOURCES)
    try:
        portal_mod._ENROLLMENT_SOURCES.pop("spd", None)
        resp = client.get("/api/portal/me/enrollments/all", headers=resident["headers"])
        assert resp.status_code == 200, resp.text
        assert {i["source"] for i in resp.json()} == {"platform"}
    finally:
        portal_mod._ENROLLMENT_SOURCES.clear()
        portal_mod._ENROLLMENT_SOURCES.update(kept)


def test_聚合源注册是幂等的():
    from app.routers.portal import _ENROLLMENT_SOURCES, register_enrollment_source

    before = len(_ENROLLMENT_SOURCES)
    register_enrollment_source("platform", _ENROLLMENT_SOURCES["platform"])
    register_enrollment_source("platform", _ENROLLMENT_SOURCES["platform"])
    assert len(_ENROLLMENT_SOURCES) == before
