"""第十轮 P0：敏感读留痕的查询接口。

第九轮建了 access_logs，每次调阅患者档案记下"谁凭什么看了谁"，但**只写没有
读**——一张答不出问题的合规日志等于没建。这个文件验证补上的查询接口：
监管能查、患者本人能查、查询动作本身也留痕、越权读被拦。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog, SmsCode


def _login(client, username, password="pw123456"):
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _clear_cooldown(phone):
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def _send_code(client, phone, purpose="login"):
    _clear_cooldown(phone)
    r = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": purpose})
    assert r.status_code == 200, r.text
    return r.json()["debug_code"]


def _portal_login(client, phone):
    code = _send_code(client, phone)
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    admin = _login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "留痕演示医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for name, role in (("al_doc", "doctor"), ("al_dir", "director")):
        client.post(
            "/api/users",
            json={"username": name, "password": "pw123456", "full_name": name,
                  "role": role, "org_id": org["id"]},
            headers=admin,
        )
    # 患者留手机号 → 居民端登录即自动实名绑定
    patient = client.post(
        "/api/patients",
        json={"name": "留痕患者", "id_card": "330782199001011234", "phone": "13700020001"},
        headers=admin,
    ).json()
    doc = _login(client, "al_doc")
    # 建立服务关系后调阅，产生两条留痕（doc·encounter，admin·global）
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "encounter_type": "outpatient"},
        headers=doc,
    )
    client.get(f"/api/archive/{patient['ehc_no']}", headers=doc)
    client.get(f"/api/archive/{patient['ehc_no']}", headers=admin)
    return {"admin": admin, "org": org, "doc": doc, "dir": _login(client, "al_dir"),
            "patient": patient}


# ================================================================ 监管视角


def test_院长能查某患者被谁看过(client, world):
    r = client.get("/api/access-logs", params={"patient_id": world["patient"]["id"]},
                   headers=world["dir"])
    assert r.status_code == 200
    viewers = {row["viewer"]: row["basis"] for row in r.json()}
    assert viewers.get("al_doc") == "encounter", "调阅人与依据没记全"
    assert viewers.get("admin") == "global"
    # 依据有可读名，翻日志的人不必记英文词
    assert any(row["basis_name"] == "本机构就诊" for row in r.json())


def test_按调阅人筛选(client, world):
    r = client.get("/api/access-logs", params={"username": "al_doc"}, headers=world["dir"])
    assert r.status_code == 200
    assert all(row["viewer"] == "al_doc" for row in r.json())
    assert r.json(), "该调阅人应有记录"


def test_查询某患者的调阅记录这个动作本身也留痕(client, world):
    """查'谁看过这个人'同样是在看这个人的隐私，必须留痕。"""
    pid = world["patient"]["id"]
    db = SessionLocal()
    try:
        before = db.query(AccessLog).filter(
            AccessLog.resource == "access_log_view", AccessLog.patient_id == pid
        ).count()
    finally:
        db.close()
    client.get("/api/access-logs", params={"patient_id": pid}, headers=world["dir"])
    db = SessionLocal()
    try:
        after = db.query(AccessLog).filter(
            AccessLog.resource == "access_log_view", AccessLog.patient_id == pid
        ).count()
        row = (
            db.query(AccessLog)
            .filter(AccessLog.resource == "access_log_view", AccessLog.patient_id == pid)
            .order_by(AccessLog.id.desc())
            .first()
        )
    finally:
        db.close()
    assert after == before + 1, "针对单个患者的调阅记录查询没有留痕"
    assert row.username == "al_dir"


def test_全表浏览不因无患者而报错也不硬记留痕(client, world):
    """不带 patient_id 的浏览：access_logs.patient_id 非空，记不了单一患者，
    故这类监管巡检不写 access_log_view，但接口本身要正常返回。"""
    db = SessionLocal()
    try:
        before = db.query(AccessLog).filter(AccessLog.resource == "access_log_view").count()
    finally:
        db.close()
    r = client.get("/api/access-logs", headers=world["dir"])
    assert r.status_code == 200 and r.json()
    db = SessionLocal()
    try:
        after = db.query(AccessLog).filter(AccessLog.resource == "access_log_view").count()
    finally:
        db.close()
    assert after == before, "全表浏览不该硬记一条 access_log_view"


def test_统计按依据汇总(client, world):
    r = client.get("/api/access-logs/stats", params={"patient_id": world["patient"]["id"]},
                   headers=world["dir"])
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    bases = {b["basis"] for b in body["by_basis"]}
    assert "encounter" in bases and "global" in bases


# ================================================================ 权限边界


def test_普通医生不能用监管查询(client, world):
    """留痕是给监管和患者本人的，不是给同事互相查的。"""
    assert client.get("/api/access-logs", headers=world["doc"]).status_code == 403
    assert client.get("/api/access-logs/stats", headers=world["doc"]).status_code == 403


# ================================================================ 患者视角


def test_患者本人能查谁看过我的档案(client, world):
    """《个保法》第 44 条知情权：本人能看到自己的档案被谁调阅过。"""
    me = _portal_login(client, "13700020001")
    r = client.get("/api/access-logs/mine", headers=me)
    assert r.status_code == 200
    rows = r.json()
    assert rows, "本人应看得到自己被调阅的记录"
    assert all(row["patient_id"] == world["patient"]["id"] for row in rows), \
        "居民端只能看到本人的记录"
    assert any(row["viewer"] == "al_doc" for row in rows)


def test_业务令牌不能走居民端本人接口(client, world):
    """/mine 走居民端令牌（scope=portal）；业务端令牌在此应被拒。"""
    r = client.get("/api/access-logs/mine", headers=world["doc"])
    assert r.status_code in (401, 403), f"业务令牌不该能读 /mine：{r.status_code}"


def test_居民令牌不能走监管接口(client, world):
    """反向：居民端令牌不能读全量监管查询。"""
    me = _portal_login(client, "13700020001")
    assert client.get("/api/access-logs", headers=me).status_code in (401, 403)
