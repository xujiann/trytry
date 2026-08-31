"""个保法落地（工程包 E2）：知情同意采集、更正权/删除权（注销）流程、
未成年人监护要件、家庭代管双因子加固（TECH_DEBT P1-2）。

按业务模块命名（不沿用 test_stageN 旧习惯）。断言尽量落到**数据与留痕**上
（患者字段真的变了、AuditLog/AccessLog 真的有记录、撤回行还在），
而不是只看状态码。
"""
from datetime import date

import pytest

from app.database import SessionLocal
from app.models import AccessLog, AuditLog, ConsentRecord, Patient, SmsCode
from app.routers.portal import _reset_portal_failures
from app.sms import set_sms_provider

MINOR_BIRTH = f"{date.today().year - 8}-01-01"    # 现龄 8 岁 < 14
ADULT_BIRTH = f"{date.today().year - 30}-01-01"   # 现龄 30 岁


@pytest.fixture(autouse=True)
def clean_state():
    _reset_portal_failures()
    set_sms_provider(None)
    yield
    _reset_portal_failures()
    set_sms_provider(None)


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    """甲乙两家机构各一名医生 + 一名 director；患者只在甲院有就诊记录。"""
    admin = _login(client, "admin", "admin123")
    a = client.post(
        "/api/organizations",
        json={"name": "同意甲县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    b = client.post(
        "/api/organizations",
        json={"name": "同意乙卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    for name, role, org in (
        ("consent_doc_a", "doctor", a),
        ("consent_doc_b", "doctor", b),
        ("consent_director", "director", a),
    ):
        client.post(
            "/api/users",
            json={"username": name, "password": "pw123456", "full_name": name,
                  "role": role, "org_id": org["id"]},
            headers=admin,
        )
    patient = client.post(
        "/api/patients",
        json={"name": "同意患者", "id_card": "330782199001010011", "birth_date": ADULT_BIRTH},
        headers=admin,
    ).json()
    doc_a = _login(client, "consent_doc_a")
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
        headers=doc_a,
    )
    return {
        "admin": admin, "a": a, "b": b, "patient": patient,
        "doc_a": doc_a, "doc_b": _login(client, "consent_doc_b"),
        "director": _login(client, "consent_director"),
    }


# ---------------------------------------------------------------- 居民端登录辅助


def _clear_cooldown(phone: str) -> None:
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def portal_login(client, phone: str) -> dict:
    _clear_cooldown(phone)
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    body = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


# ================================================================ 知情同意采集


def test_窗口代录缺佐证被拒422(client, world):
    resp = client.post(
        "/api/consents",
        json={"patient_id": world["patient"]["id"], "scene": "archive", "evidence": "  "},
        headers=world["admin"],
    )
    assert resp.status_code == 422
    assert "佐证" in resp.json()["detail"]


def test_窗口代录落库并自动引用当前文本版本(client, world):
    resp = client.post(
        "/api/consents",
        json={"patient_id": world["patient"]["id"], "scene": "archive",
              "evidence": "签字影像附件#101"},
        headers=world["doc_a"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 文本版本自动取该场景当前生效版（种子 v1）——事后答得出"同意的是哪段话"
    assert body["method"] == "proxy" and body["text_version"] == "v1"
    assert body["operator_user_id"] is not None and body["revoked_at"] == ""
    # 种子文本确实存在且能对回全文
    texts = client.get("/api/consents/texts?scene=archive", headers=world["admin"]).json()
    assert [t["version"] for t in texts] == ["v1"] and texts[0]["content"]


def test_按患者查询同意须过可见性且留痕(client, world):
    pid = world["patient"]["id"]
    # 乙院医生与该患者无任何业务关系：403
    denied = client.get(f"/api/consents?patient_id={pid}", headers=world["doc_b"])
    assert denied.status_code == 403
    # 甲院医生（有就诊关系）：可见，且 AccessLog 记下 resource=consent 的这一笔
    ok = client.get(f"/api/consents?patient_id={pid}", headers=world["doc_a"])
    assert ok.status_code == 200
    assert any(r["scene"] == "archive" for r in ok.json())
    with SessionLocal() as db:
        logs = (
            db.query(AccessLog)
            .filter(
                AccessLog.username == "consent_doc_a",
                AccessLog.patient_id == pid,
                AccessLog.resource == "consent",
            )
            .all()
        )
        assert logs and logs[-1].basis == "encounter"


def test_撤回置时间戳不删行且不可重复撤回(client, world):
    pid = world["patient"]["id"]
    created = client.post(
        "/api/consents",
        json={"patient_id": pid, "scene": "followup", "evidence": "短信确认流水#202"},
        headers=world["admin"],
    ).json()
    revoked = client.post(f"/api/consents/{created['id']}/revoke", headers=world["admin"])
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] != ""
    # 撤回不删行：记录仍可查到，且带撤回时刻（撤回本身要可举证）
    rows = client.get(f"/api/consents?patient_id={pid}&scene=followup", headers=world["admin"]).json()
    assert rows and rows[0]["id"] == created["id"] and rows[0]["revoked_at"] != ""
    assert client.post(
        f"/api/consents/{created['id']}/revoke", headers=world["admin"]
    ).status_code == 409


def test_未成年人窗口登记同意缺监护人422(client, world):
    minor = client.post(
        "/api/patients",
        json={"name": "同意幼童", "id_card": "330782201801010022", "birth_date": MINOR_BIRTH},
        headers=world["admin"],
    ).json()
    lacking = client.post(
        "/api/consents",
        json={"patient_id": minor["id"], "scene": "archive", "evidence": "签字影像#301"},
        headers=world["admin"],
    )
    assert lacking.status_code == 422
    assert "监护人" in lacking.json()["detail"]
    ok = client.post(
        "/api/consents",
        json={"patient_id": minor["id"], "scene": "archive", "evidence": "签字影像#301",
              "guardian_name": "幼童母亲", "guardian_id_card": "330782199001010033",
              "guardian_relation": "mother"},
        headers=world["admin"],
    )
    assert ok.status_code == 201
    # 监护人证件号出口脱敏（保留前4后4）
    assert ok.json()["guardian_id_card"] == "3307**********0033"


# ================================================================ 居民端自签与查询


@pytest.fixture(scope="module")
def me(client, world):
    """居民本人：手机号登录后自动实名绑定到自己的档案。"""
    patient = client.post(
        "/api/patients",
        json={"name": "同意本人", "id_card": "330782199201010044",
              "birth_date": ADULT_BIRTH, "phone": "13800020001"},
        headers=world["admin"],
    ).json()
    return {"patient": patient, "headers": portal_login(client, "13800020001")}


def test_居民端本人自签与本人查询(client, world, me):
    signed = client.post(
        "/api/portal/me/consents", json={"scene": "family_contract"}, headers=me["headers"]
    )
    assert signed.status_code == 201, signed.text
    body = signed.json()
    assert body["method"] == "self" and body["text_version"] == "v1"
    assert body["resident_account_id"] is not None and body["operator_user_id"] is None
    mine = client.get("/api/portal/me/consents", headers=me["headers"]).json()
    assert any(r["scene"] == "family_contract" and r["method"] == "self" for r in mine)


def test_未实名绑定账户不能自签(client, world):
    headers = portal_login(client, "13800020009")
    resp = client.post(
        "/api/portal/me/consents", json={"scene": "archive"}, headers=headers
    )
    assert resp.status_code == 403


# ================================================================ 更正权全链


def test_更正流程全链_申请审核变更审计(client, world, me):
    # 1) 居民端提交更正申请（改姓名与电话——白名单字段）
    submitted = client.post(
        "/api/portal/me/corrections",
        json={"changes": {"name": "同意本人甲", "phone": "13800020002"},
              "reason": "身份证姓名有误且换了号码"},
        headers=me["headers"],
    )
    assert submitted.status_code == 201, submitted.text
    req_id = submitted.json()["id"]
    assert submitted.json()["status"] == "pending" and submitted.json()["source"] == "portal"

    # 2) director 在待审清单里看得到
    pending = client.get("/api/consents/corrections?status=pending", headers=world["director"]).json()
    assert any(r["id"] == req_id for r in pending)

    # 3) 审核通过 → patients 字段真的变了
    reviewed = client.post(
        f"/api/consents/corrections/{req_id}/review",
        json={"approve": True, "comment": "核对户口本无误"},
        headers=world["director"],
    )
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved"
    with SessionLocal() as db:
        patient = db.get(Patient, me["patient"]["id"])
        assert patient.name == "同意本人甲" and patient.phone == "13800020002"
        assert patient.id_card == me["patient"]["id_card"]  # 证件号纹丝不动
        # 4) 审计可查：审核这笔写操作落了 AuditLog（现有审计中间件覆盖）
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.path == f"/api/consents/corrections/{req_id}/review")
            .all()
        )
        assert audit and audit[-1].username == "consent_director" and audit[-1].status_code == 200

    # 5) 居民端能看到申请进度
    mine = client.get("/api/portal/me/corrections", headers=me["headers"]).json()
    assert any(r["id"] == req_id and r["status"] == "approved" for r in mine)
    # 6) 已处理的申请不能重复审核
    assert client.post(
        f"/api/consents/corrections/{req_id}/review",
        json={"approve": False, "comment": "重复"},
        headers=world["director"],
    ).status_code == 409


def test_拒绝申请必须写审核意见(client, world, me):
    req = client.post(
        "/api/portal/me/corrections",
        json={"changes": {"gender": "女"}, "reason": "登记性别有误"},
        headers=me["headers"],
    ).json()
    no_comment = client.post(
        f"/api/consents/corrections/{req['id']}/review",
        json={"approve": False}, headers=world["director"],
    )
    assert no_comment.status_code == 422
    rejected = client.post(
        f"/api/consents/corrections/{req['id']}/review",
        json={"approve": False, "comment": "需先提供证明材料"},
        headers=world["director"],
    )
    assert rejected.status_code == 200 and rejected.json()["review_comment"] == "需先提供证明材料"
    with SessionLocal() as db:  # 拒绝不改档案
        assert db.get(Patient, me["patient"]["id"]).gender != "女"


def test_身份证号不在更正白名单(client, world, me):
    resp = client.post(
        "/api/portal/me/corrections",
        json={"changes": {"id_card": "330782199201010055"}, "reason": "想换证件号"},
        headers=me["headers"],
    )
    assert resp.status_code == 422
    assert "id_card" in resp.json()["detail"]


# ================================================================ 删除权（注销）


def test_注销后检索不可见而历史照常可查(client, world):
    admin = world["admin"]
    gone = client.post(
        "/api/patients",
        json={"name": "注销患者", "id_card": "330782198501010066", "birth_date": ADULT_BIRTH},
        headers=admin,
    ).json()
    client.post(
        "/api/encounters",
        json={"patient_id": gone["id"], "org_id": world["a"]["id"], "encounter_type": "outpatient"},
        headers=world["doc_a"],
    )
    # 窗口代提注销申请 → director 审核通过
    req = client.post(
        "/api/consents/corrections",
        json={"patient_id": gone["id"], "request_type": "deactivate", "reason": "本人申请注销档案"},
        headers=admin,
    ).json()
    approved = client.post(
        f"/api/consents/corrections/{req['id']}/review",
        json={"approve": True, "comment": "已当面确认"},
        headers=world["director"],
    )
    assert approved.status_code == 200
    with SessionLocal() as db:
        assert db.get(Patient, gone["id"]).deactivated_at is not None

    # 检索不再出现（新业务入口挡住）
    found = client.get("/api/patients?keyword=注销患者", headers=admin).json()
    assert all(p["id"] != gone["id"] for p in found)
    # 既有业务历史照常可查：按 ehc_no 直取与就诊记录都在（医疗记录法定保留）
    assert client.get(f"/api/patients/{gone['ehc_no']}", headers=admin).status_code == 200
    encounters = client.get(
        f"/api/encounters?patient_id={gone['id']}", headers=world["doc_a"]
    ).json()
    assert encounters
    # 居民端也绑不上已注销档案（绑定入口过滤）
    headers = portal_login(client, "13800020010")
    bind = client.post(
        "/api/portal/auth/realname",
        json={"name": "注销患者", "id_card": "330782198501010066"},
        headers=headers,
    )
    assert bind.status_code == 404


# ================================================================ 代管双因子（P1-2）


def test_无手机号档案代管需窗口授权_无则428有则通过(client, world, me):
    admin = world["admin"]
    elder = client.post(
        "/api/patients",
        json={"name": "同意老人", "id_card": "330782194501010077", "birth_date": ADULT_BIRTH},
        headers=admin,
    ).json()
    # 第一道尝试：仅凭姓名+身份证号（单因子）→ 428 提示到窗口办理
    blocked = client.post(
        "/api/portal/me/family",
        json={"name": "同意老人", "id_card": "330782194501010077", "relation": "parent"},
        headers=me["headers"],
    )
    assert blocked.status_code == 428
    assert "窗口" in blocked.json()["detail"]
    # 窗口核验身份后登记代管授权（scene=family_delegate）
    client.post(
        "/api/consents",
        json={"patient_id": elder["id"], "scene": "family_delegate",
              "evidence": "窗口身份核验记录#401"},
        headers=admin,
    )
    ok = client.post(
        "/api/portal/me/family",
        json={"name": "同意老人", "id_card": "330782194501010077", "relation": "parent"},
        headers=me["headers"],
    )
    assert ok.status_code == 201, ok.text


def test_授权撤回后单因子路径重新关闭(client, world, me):
    admin = world["admin"]
    uncle = client.post(
        "/api/patients",
        json={"name": "同意亲属", "id_card": "330782195501010088", "birth_date": ADULT_BIRTH},
        headers=admin,
    ).json()
    consent = client.post(
        "/api/consents",
        json={"patient_id": uncle["id"], "scene": "family_delegate",
              "evidence": "窗口身份核验记录#402"},
        headers=admin,
    ).json()
    client.post(f"/api/consents/{consent['id']}/revoke", headers=admin)
    blocked = client.post(
        "/api/portal/me/family",
        json={"name": "同意亲属", "id_card": "330782195501010088", "relation": "other"},
        headers=me["headers"],
    )
    assert blocked.status_code == 428


def test_代管未成年人缺监护人信息422(client, world, me):
    admin = world["admin"]
    kid = client.post(
        "/api/patients",
        json={"name": "同意小孩", "id_card": "330782201801010099", "birth_date": MINOR_BIRTH},
        headers=admin,
    ).json()
    client.post(
        "/api/consents",
        json={"patient_id": kid["id"], "scene": "family_delegate",
              "evidence": "窗口身份核验记录#403",
              "guardian_name": "同意本人甲", "guardian_id_card": "330782199201010044",
              "guardian_relation": "mother"},
        headers=admin,
    )
    lacking = client.post(
        "/api/portal/me/family",
        json={"name": "同意小孩", "id_card": "330782201801010099", "relation": "child"},
        headers=me["headers"],
    )
    assert lacking.status_code == 422
    assert "监护人" in lacking.json()["detail"]
    ok = client.post(
        "/api/portal/me/family",
        json={"name": "同意小孩", "id_card": "330782201801010099", "relation": "child",
              "guardian_name": "同意本人甲", "guardian_id_card": "330782199201010044",
              "guardian_relation": "mother"},
        headers=me["headers"],
    )
    assert ok.status_code == 201, ok.text
    # 双因子 + 监护要件齐备后，代管人能替孩子自签同意（监护人代未成年人行使）
    signed = client.post(
        "/api/portal/me/consents",
        json={"scene": "chronic_enroll", "patient_id": kid["id"],
              "guardian_name": "同意本人甲", "guardian_id_card": "330782199201010044",
              "guardian_relation": "mother"},
        headers=me["headers"],
    )
    assert signed.status_code == 201
    with SessionLocal() as db:
        row = (
            db.query(ConsentRecord)
            .filter(ConsentRecord.patient_id == kid["id"], ConsentRecord.scene == "chronic_enroll")
            .one()
        )
        assert row.guardian_name == "同意本人甲" and row.guardian_relation == "mother"


def test_代管未成年人自签缺监护人422(client, world, me):
    with SessionLocal() as db:
        kid = db.query(Patient).filter(Patient.id_card == "330782201801010099").one()
    resp = client.post(
        "/api/portal/me/consents",
        json={"scene": "followup", "patient_id": kid.id},
        headers=me["headers"],
    )
    assert resp.status_code == 422
    assert "监护人" in resp.json()["detail"]
