"""E7 临床决策支持（CDSS）深化用例。

守四条：结构化过敏史读写；交互核查升级（过敏 contra + 药物相互作用 caution，
且能核查患者历史用药）；临床路径目录/入径/变异/出径闭环；诊间提醒结构。
另含横向隔离（B 院医师读 A 院患者过敏史 403）与 404/409。

交互核查所用相互作用药对来自 app/data/drug_rules_seed.py（main.py 启动时 seed）：
华法林 B01AA03 的 interactions 含阿司匹林 B01AC06。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

WARFARIN = "B01AA03"   # 华法林（既作过敏原又作相互作用一端）
ASPIRIN = "B01AC06"    # 阿司匹林（与华法林相互作用）


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("CDSS甲县医院", "lead_hospital", "county"),
            ("CDSS乙镇卫生院", "township", "township"),
        ]
    ]


def _doctor(client, admin, orgs, username, org):
    client.post(
        "/api/users",
        json={"username": username, "password": "passw0rd1", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor_a(client, admin, orgs):
    return _doctor(client, admin, orgs, "cdss_doc_a", orgs[0])


@pytest.fixture(scope="module")
def doctor_b(client, admin, orgs):
    return _doctor(client, admin, orgs, "cdss_doc_b", orgs[1])


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "CDSS患者", "id_card": "330782199001010011", "gender": "男",
              "birth_date": "1990-01-01"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def seeded_allergy(client, admin, orgs, patient):
    """admin（全域）以甲院名义登记药品过敏史。

    这一步同时把 (patient, 甲院) 写进 patient_allergies —— 它带 patient_id + org_id，
    会被可见性推导为"甲院服务过该患者"的服务关系，甲院医师据此可读该患者。
    """
    resp = client.post(
        f"/api/cdss/patients/{patient['id']}/allergies",
        json={"org_id": orgs[0]["id"], "allergen": WARFARIN, "allergen_type": "drug",
              "severity": "severe", "note": "既往用药过敏"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------- 过敏史 ----------------


def test_过敏史登记与查询(client, doctor_a, patient, seeded_allergy):
    rows = client.get(f"/api/cdss/patients/{patient['id']}/allergies", headers=doctor_a).json()
    assert any(r["allergen"] == WARFARIN and r["allergen_type"] == "drug" for r in rows)


def test_乙院医师读甲院患者过敏史被拒(client, doctor_b, patient, seeded_allergy):
    """横向隔离：乙院医师与该患者无任何业务关系，assert_patient_visible 应 403。"""
    resp = client.get(f"/api/cdss/patients/{patient['id']}/allergies", headers=doctor_b)
    assert resp.status_code == 403


def test_过敏史登记患者不存在404(client, admin, orgs):
    resp = client.post(
        "/api/cdss/patients/999999/allergies",
        json={"org_id": orgs[0]["id"], "allergen": "X"},
        headers=admin,
    )
    assert resp.status_code == 404


# ---------------- 交互核查升级 ----------------


def test_交互核查_过敏冲突与药物相互作用(client, doctor_a, patient, seeded_allergy):
    """提交华法林+阿司匹林：华法林命中过敏史(contra)，两药相互作用(caution)。"""
    resp = client.post(
        "/api/cdss/interaction-check",
        json={"patient_id": patient["id"], "drug_codes": [WARFARIN, ASPIRIN],
              "include_history": False},
        headers=doctor_a,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["advisory"] == "仅供参考"
    assert body["checked_against_history"] is False
    conflicts = body["conflicts"]
    allergy_hit = [c for c in conflicts if c["type"] == "allergy"]
    assert allergy_hit and allergy_hit[0]["severity"] == "contra"
    assert allergy_hit[0]["drug"] == WARFARIN
    inter = [c for c in conflicts if c["type"] == "interaction"]
    assert inter and inter[0]["severity"] == "caution"
    assert {inter[0]["drug"], inter[0]["against"]} == {WARFARIN, ASPIRIN}


def test_交互核查_命中患者历史用药(client, doctor_a, patient, orgs, seeded_allergy):
    """升级核心：先给患者开一张含华法林的处方，再核查阿司匹林——
    虽不在同一张处方，仍应与历史用药华法林查出相互作用(caution)。"""
    rx = client.post(
        "/api/prescriptions",
        json={"patient_id": patient["id"], "org_id": orgs[0]["id"], "diagnosis_name": "房颤",
              "items": [{"drug_code": WARFARIN, "drug_name": "华法林", "daily_dose": 3, "days": 30}]},
        headers=doctor_a,
    )
    assert rx.status_code == 201, rx.text
    resp = client.post(
        "/api/cdss/interaction-check",
        json={"patient_id": patient["id"], "drug_codes": [ASPIRIN], "include_history": True},
        headers=doctor_a,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checked_against_history"] is True
    hist = [c for c in body["conflicts"]
            if c["type"] == "interaction" and c["drug"] == ASPIRIN and c["against"] == WARFARIN]
    assert hist and hist[0]["severity"] == "caution"


# ---------------- 临床路径 ----------------


@pytest.fixture(scope="module")
def pathway(client, admin):
    resp = client.post(
        "/api/cdss/pathways",
        json={"code": "CP-HT-01", "name": "高血压门诊路径", "disease": "hypertension"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    p = resp.json()
    client.post(
        f"/api/cdss/pathways/{p['id']}/nodes",
        json={"day_no": 1, "name": "血压测量", "order_type": "exam", "detail": "首诊测压"},
        headers=admin,
    )
    return p


def test_路径目录节点建立(client, admin, pathway):
    nodes = client.get(f"/api/cdss/pathways/{pathway['id']}/nodes", headers=admin).json()
    assert nodes and nodes[0]["order_type"] == "exam"
    listed = client.get("/api/cdss/pathways?active=true", headers=admin).json()
    assert any(p["code"] == "CP-HT-01" for p in listed)


def test_路径编码重复409(client, admin):
    resp = client.post(
        "/api/cdss/pathways",
        json={"code": "CP-HT-01", "name": "重复", "disease": "x"},
        headers=admin,
    )
    assert resp.status_code == 409


def test_路径目录限管理员(client, doctor_a):
    resp = client.post(
        "/api/cdss/pathways",
        json={"code": "CP-X", "name": "越权", "disease": "x"},
        headers=doctor_a,
    )
    assert resp.status_code == 403


def test_入径_变异_出径闭环(client, doctor_a, orgs, patient, pathway, seeded_allergy):
    enroll = client.post(
        "/api/cdss/patient-pathways",
        json={"patient_id": patient["id"], "org_id": orgs[0]["id"], "pathway_id": pathway["id"],
              "enrolled_date": "2026-08-17"},
        headers=doctor_a,
    )
    assert enroll.status_code == 201, enroll.text
    pp = enroll.json()
    assert pp["status"] == "in_path"

    listed = client.get(
        f"/api/cdss/patient-pathways?patient_id={patient['id']}", headers=doctor_a
    ).json()
    assert any(x["id"] == pp["id"] for x in listed)

    var = client.post(
        f"/api/cdss/patient-pathways/{pp['id']}/variance",
        json={"node_id": 0, "reason": "合并感染需调整"},
        headers=doctor_a,
    )
    assert var.status_code == 201 and var.json()["status"] == "variant"

    done = client.patch(
        f"/api/cdss/patient-pathways/{pp['id']}/complete", headers=doctor_a
    )
    assert done.status_code == 200 and done.json()["status"] == "completed"


def test_入径记录不存在404(client, doctor_a):
    resp = client.patch("/api/cdss/patient-pathways/999999/complete", headers=doctor_a)
    assert resp.status_code == 404


# ---------------- 诊间提醒 ----------------


def test_诊间提醒返回结构(client, doctor_a, patient, seeded_allergy):
    resp = client.get(f"/api/cdss/reminders/{patient['id']}", headers=doctor_a)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reminders" in body and isinstance(body["reminders"], list)


# ---------------- S12 临床路径自动开立医嘱 ----------------


@pytest.fixture(scope="module")
def pathway_multi(client, admin):
    """含 2 个节点的路径，用于验证入径时逐节点自动开立医嘱。"""
    resp = client.post(
        "/api/cdss/pathways",
        json={"code": "CP-S12-01", "name": "S12自动开立路径", "disease": "s12"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    p = resp.json()
    for node in [
        {"day_no": 1, "name": "血常规", "order_type": "lab", "detail": "入径首日"},
        {"day_no": 2, "name": "复查血压", "order_type": "exam", "detail": "次日复查"},
    ]:
        r = client.post(
            f"/api/cdss/pathways/{p['id']}/nodes", json=node, headers=admin
        )
        assert r.status_code == 201, r.text
    return p


def test_入径自动生成医嘱(client, doctor_a, orgs, patient, pathway_multi, seeded_allergy):
    enroll = client.post(
        "/api/cdss/patient-pathways",
        json={"patient_id": patient["id"], "org_id": orgs[0]["id"],
              "pathway_id": pathway_multi["id"], "enrolled_date": "2026-08-17"},
        headers=doctor_a,
    )
    assert enroll.status_code == 201, enroll.text
    body = enroll.json()
    # 路径有 2 个节点 → 自动生成 2 条医嘱
    assert body["orders_generated"] == 2
    assert len(body["orders"]) == 2
    assert all(o["status"] == "ordered" for o in body["orders"])
    assert {o["name"] for o in body["orders"]} == {"血常规", "复查血压"}

    # GET 医嘱清单能读到
    got = client.get(
        f"/api/cdss/patient-pathways/{body['id']}/orders", headers=doctor_a
    )
    assert got.status_code == 200, got.text
    rows = got.json()
    assert len(rows) == 2
    # 按 day_no, id 排序
    assert rows[0]["day_no"] == 1 and rows[1]["day_no"] == 2


def test_执行与作废路径医嘱(client, admin, doctor_a, orgs, pathway_multi):
    # 用独立患者避免与其它入径用例撞"同患者同路径重复入径"幂等拦截；
    # 入径经全域 admin（免建关系），机构归属仍是 orgs[0]，执行由 orgs[0] 的 doctor_a。
    p = client.post("/api/patients", json={"name": "路径医嘱患者", "id_card": "330100199404040044"}, headers=admin).json()
    enroll = client.post(
        "/api/cdss/patient-pathways",
        json={"patient_id": p["id"], "org_id": orgs[0]["id"],
              "pathway_id": pathway_multi["id"], "enrolled_date": "2026-08-17"},
        headers=admin,
    ).json()
    orders = enroll["orders"]

    # 执行第一条
    ex = client.patch(
        f"/api/cdss/patient-pathway-orders/{orders[0]['id']}/execute", headers=doctor_a
    )
    assert ex.status_code == 200, ex.text
    assert ex.json()["status"] == "executed"

    # 再次执行 → 409（已非 ordered）
    ex2 = client.patch(
        f"/api/cdss/patient-pathway-orders/{orders[0]['id']}/execute", headers=doctor_a
    )
    assert ex2.status_code == 409

    # 作废第二条
    ca = client.patch(
        f"/api/cdss/patient-pathway-orders/{orders[1]['id']}/cancel", headers=doctor_a
    )
    assert ca.status_code == 200, ca.text
    assert ca.json()["status"] == "cancelled"


def test_跨机构医师执行他院路径医嘱被拒(
    client, admin, doctor_b, orgs, pathway_multi
):
    """入径归甲院；乙院医师执行其医嘱，assert_obj_org_writable 应 403。"""
    p = client.post("/api/patients", json={"name": "跨机构路径患者", "id_card": "330100199405050055"}, headers=admin).json()
    enroll = client.post(
        "/api/cdss/patient-pathways",
        json={"patient_id": p["id"], "org_id": orgs[0]["id"],
              "pathway_id": pathway_multi["id"], "enrolled_date": "2026-08-17"},
        headers=admin,
    ).json()
    order_id = enroll["orders"][0]["id"]
    resp = client.patch(
        f"/api/cdss/patient-pathway-orders/{order_id}/execute", headers=doctor_b
    )
    assert resp.status_code == 403


def test_路径医嘱不存在404(client, doctor_a):
    resp = client.patch(
        "/api/cdss/patient-pathway-orders/999999/execute", headers=doctor_a
    )
    assert resp.status_code == 404


def test_enroll_idempotent(client, admin, orgs, pathway):
    """LOW-MED-5 回归：同患者同路径重复入径 → 409（避免双份医嘱/双份提醒）。"""
    p = client.post("/api/patients", json={"name": "入径幂等患者", "id_card": "330100199303030033"}, headers=admin).json()
    body = {"patient_id": p["id"], "org_id": orgs[0]["id"], "pathway_id": pathway["id"]}
    first = client.post("/api/cdss/patient-pathways", json=body, headers=admin)
    assert first.status_code in (200, 201), first.text
    second = client.post("/api/cdss/patient-pathways", json=body, headers=admin)
    assert second.status_code == 409, second.text
