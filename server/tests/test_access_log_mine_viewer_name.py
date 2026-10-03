"""居民端「谁看过我的档案」的调阅人给姓名、不给登录账号（P2-1248，第三十六批扫描 U1-1 的 clear 那一半）。

`GET /api/access-logs/mine` 与监管清单共用 `_row_out`，`viewer = log.username`：凡是看过这位居民档案的医生、管理层，
**登录账号**都原样送到居民手机上（m.js 印成「调阅人」，修前实测 `{'viewer': 'wang_doc07', ...}`）——登录名回答不了
「谁看过我」，还能被拿去试口令（员工锁号只按用户名计，居民连错 5 次就把医生锁 10 分钟；锁号属认证链路，不在本条）。
兄弟路径慢专病居民端的主管医生（P2-560）写明「只取姓名……不拿登录账号顶替」。

修法：`/mine` 经 `AccessLog.user_id` 取 `users.full_name`，没填姓名的给角色中文名（角色表里的名字，自定义角色同样）；
居民端主体（`resident:{账户号}` / `portal:legacy`）按依据给「家庭代管账户」「本人」。监管清单 `/api/access-logs`
照旧给登录账号（稽核按账号查）。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import SmsCode, User
from app.visibility import _write_access_log, log_resident_access

ELDER_PHONE, CHILD_PHONE = "13700121248", "13700121249"


def _portal_login(client, phone):
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    assert body["bound"] is True, body
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21248 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    role = client.post("/api/rbac/roles", headers=admin, json={"key": "p21248_nurse", "name": "护理员"})
    assert role.status_code == 201, role.text
    for username, role_key, full_name in (("p21248_doc", "doctor", "王医生"), ("p21248_doc2", "doctor", ""),
                                          ("p21248_nurse", "p21248_nurse", "")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role_key, "org_id": org, "full_name": full_name})
        assert resp.status_code == 201, resp.text
    elder = client.post("/api/patients", headers=admin, json={
        "name": "P21248 老人", "id_card": "330424195004041248", "phone": ELDER_PHONE}).json()
    client.post("/api/patients", headers=admin, json={
        "name": "P21248 子女", "id_card": "330424198005051249", "phone": CHILD_PHONE})

    # 两位医生凭本机构就诊调阅（一位填了姓名，一位没填）
    for username in ("p21248_doc", "p21248_doc2"):
        doc = login(client, username, "passw0rd1")
        assert client.post("/api/encounters", headers=doc, json={
            "patient_id": elder["id"], "org_id": org, "encounter_type": "outpatient"}).status_code == 201
        assert client.get(f"/api/archive/{elder['ehc_no']}", headers=doc).status_code == 200
    # 自定义角色、没填姓名的账号（直接走业务端留痕底座）
    with SessionLocal() as db:
        nurse = db.query(User).filter(User.username == "p21248_nurse").one()
        _write_access_log(nurse, elder["id"], "archive", "authorization")

    me = _portal_login(client, ELDER_PHONE)
    # 子女账号代管老人（老人档案有手机号：要老人这个号收到的验证码），再读老人的档案 → resident:{子女账号}、依据 delegate
    child = _portal_login(client, CHILD_PHONE)
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == ELDER_PHONE).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": ELDER_PHONE, "purpose": "bind"}).json()["debug_code"]
    resp = client.post("/api/portal/me/family", headers=child, json={
        "name": "P21248 老人", "id_card": "330424195004041248", "relation": "parent", "code": code})
    assert resp.status_code == 201, resp.text
    assert client.get(f"/api/portal/me/archive?patient_id={elder['id']}", headers=child).status_code == 200
    # 已停用的双因子查档通道（无账户，主体 portal:legacy、依据 self），直接走居民端留痕底座
    log_resident_access(None, elder["id"], "archive", "self")
    child_account = client.get("/api/portal/me", headers=child).json()["account_id"]
    return {"elder": elder, "me": me, "child_account": child_account}


def test_mine_调阅人给姓名_没填姓名给角色名_居民主体给称呼(client, world):
    resp = client.get("/api/access-logs/mine", headers=world["me"])
    assert resp.status_code == 200, resp.text
    viewers = [(r["viewer"], r["basis"]) for r in resp.json()]
    assert viewers == [                                  # id 倒序
        ("本人", "self"),                                # portal:legacy
        ("家庭代管账户", "delegate"),                    # resident:{子女账号}
        ("护理员", "authorization"),                      # 自定义角色、没填姓名：角色表里的名字
        ("医师", "encounter"),                            # 没填姓名：内置角色中文名
        ("王医生", "encounter"),                          # 修前是 p21248_doc
    ], viewers


def test_mine_响应里不出现任何登录账号(client, world):
    text = client.get("/api/access-logs/mine", headers=world["me"]).text
    with SessionLocal() as db:
        usernames = [u for (u,) in db.query(User.username).all()]
    assert "p21248_doc" in usernames
    leaked = [u for u in usernames if f'"{u}"' in text]
    assert not leaked, leaked                            # 修前 p21248_doc、p21248_doc2、p21248_nurse 都在
    assert "resident:" not in text and "portal:legacy" not in text


def test_监管清单照旧给登录账号(client, admin, world):
    rows = client.get(f"/api/access-logs?patient_id={world['elder']['id']}", headers=admin).json()
    assert [r["viewer"] for r in rows] == [
        "portal:legacy", f"resident:{world['child_account']}", "p21248_nurse", "p21248_doc2", "p21248_doc"]
