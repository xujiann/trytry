"""第十二轮：书稿审读揪出的四条"登记在案的差距"逐一关账。

1. 一份档案只绑一个账户——应用层查重是 check-then-act（D-2 同形），
   下沉为 patient_id 部分唯一索引 + 撞约束翻译 409。
2. 留痕统计按患者聚焦时自我留痕——判准是"是否指向可识别的个人"，
   此前只有清单查询记、统计不记。
3. 病理拒收原因强制五类标准项——自由文本无法按原因分解统计。
4. 医废暂存点位须属同一机构——跨机构暂存等于记到别家的暂存间头上。
"""
import threading

import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import AccessLog, ResidentAccount, SmsCode


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "十二轮县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "十二轮居民", "id_card": "320000199505055555"},
        headers=admin,
    ).json()


def _clear_cooldown(phone: str) -> None:
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def _portal_login(client, phone: str) -> dict:
    _clear_cooldown(phone)
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _mk_patient(client, admin, name: str, id_card: str) -> dict:
    resp = client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ---------------------------------------------------------------- 1. 绑定唯一性


def test_串行重复绑定仍是可读的409(client, admin):
    _mk_patient(client, admin, "串行绑定居民", "330782199404044567")
    h1 = _portal_login(client, "13600128001")
    h2 = _portal_login(client, "13600128002")
    body = {"name": "串行绑定居民", "id_card": "330782199404044567"}
    assert client.post("/api/portal/auth/realname", json=body, headers=h1).status_code == 200
    dup = client.post("/api/portal/auth/realname", json=body, headers=h2)
    assert dup.status_code == 409
    assert "已被其他账号绑定" in dup.json()["detail"]


def test_并发实名绑定同一档案只成一个(client, admin):
    """六个账户用栅栏同时绑同一份档案：恰好一个 200、其余 409，
    库里该档案的绑定数为 1。改回"只留应用层查重"（去掉唯一索引），
    本用例必红——多个都 200、绑定数 > 1。二路并发窗口开不稳定，
    压到六路才可靠触发（第 17 章：并发度要压到能触发缺陷的量级）。"""
    patient = _mk_patient(client, admin, "并发绑定居民", "330782199303033456")
    headers_list = [_portal_login(client, f"1360012{i:04d}") for i in range(6)]

    barrier = threading.Barrier(len(headers_list))
    results: list[int] = []
    lock = threading.Lock()
    body = {"name": "并发绑定居民", "id_card": "330782199303033456"}

    def worker(headers):
        barrier.wait()
        resp = client.post("/api/portal/auth/realname", json=body, headers=headers)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(h,)) for h in headers_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200] + [409] * 5, f"并发绑定响应码：{sorted(results)}"
    with SessionLocal() as db:
        bound = (
            db.query(ResidentAccount)
            .filter(ResidentAccount.patient_id == patient["id"])
            .count()
        )
    assert bound == 1, f"一份档案被 {bound} 个账户绑定"


# ---------------------------------------------------------------- 2. 统计聚焦自我留痕


def test_留痕统计按患者聚焦时自我留痕(client, admin, patient):
    # 先制造一条常规调阅留痕，统计才有内容
    client.get(f"/api/patients/{patient['id']}/archive", headers=admin)

    def view_logs_count() -> int:
        with SessionLocal() as db:
            return (
                db.query(AccessLog)
                .filter(
                    AccessLog.patient_id == patient["id"],
                    AccessLog.resource == "access_log_view",
                )
                .count()
            )

    before = view_logs_count()
    resp = client.get(
        "/api/access-logs/stats", params={"patient_id": patient["id"]}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert view_logs_count() == before + 1, "按患者聚焦的统计没有自我留痕"

    # 不带患者定位的全局聚合不自我记录
    global_before = view_logs_count()
    assert client.get("/api/access-logs/stats", headers=admin).status_code == 200
    assert view_logs_count() == global_before


# ---------------------------------------------------------------- 3. 拒收原因五类


def test_病理拒收原因限定标准五类(client, admin, org, patient):
    request = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"],
              "center_type": "pathology", "item_code": "P001",
              "item_name": "组织病理学检查"},
        headers=admin,
    ).json()
    specimen = client.post(
        "/api/pathology/specimens",
        json={"request_id": request["id"], "site": "胃体"},
        headers=admin,
    ).json()

    free_text = client.post(
        f"/api/pathology/specimens/{specimen['id']}/reject",
        json={"reject_reason": "看着不太行"},
        headers=admin,
    )
    assert free_text.status_code == 422
    assert "标准项" in free_text.json()["detail"]

    standard = client.post(
        f"/api/pathology/specimens/{specimen['id']}/reject",
        json={"reject_reason": "标本量不足"},
        headers=admin,
    )
    assert standard.status_code == 200
    assert standard.json()["status"] == "rejected"


# ---------------------------------------------------------------- 4. 暂存点位同机构


def test_医废暂存点位须属同一机构(client, admin, org):
    other = client.post(
        "/api/organizations",
        json={"name": "十二轮乙卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    source = client.post(
        "/api/medwaste/locations",
        json={"org_id": org["id"], "name": "外科处置室", "location_type": "source"},
        headers=admin,
    ).json()
    cross_storage = client.post(
        "/api/medwaste/locations",
        json={"org_id": other["id"], "name": "别家的暂存间", "location_type": "storage"},
        headers=admin,
    ).json()
    own_storage = client.post(
        "/api/medwaste/locations",
        json={"org_id": org["id"], "name": "自家的暂存间", "location_type": "storage"},
        headers=admin,
    ).json()
    waste = client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "sharp", "weight_kg": 0.5,
              "collected_date": "2026-08-14", "source_location_id": source["id"]},
        headers=admin,
    ).json()

    cross = client.post(
        f"/api/medwaste/{waste['id']}/store",
        json={"storage_location_id": cross_storage["id"]},
        headers=admin,
    )
    assert cross.status_code == 422
    assert "不属于" in cross.json()["detail"]

    ok = client.post(
        f"/api/medwaste/{waste['id']}/store",
        json={"storage_location_id": own_storage["id"]},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
