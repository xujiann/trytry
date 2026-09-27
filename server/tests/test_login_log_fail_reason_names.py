"""登录留痕页把失败原因的代号原样显示：bad_credentials / disabled / code_401（P2-427）。

`login_logs.fail_reason` 存的是代号（§4 状态裸字符串），登录留痕页原样显示——管理层查爆破画像看到的是
「bad_credentials」「totp_invalid」「code_401」。修后出参多一个 `fail_reason_name`（文案表 `LOGIN_FAIL_REASON_NAMES`，
措辞照抄拒绝登录时回给用户的那句话；居民端短信验码失败的 `code_<状态码>` 按状态码现拼），页面显示它；
文案表与模型列注释的对照由 `test_status_text_from_backend.py` 盯着。
"""
from app.routers.users import LOGIN_FAIL_REASON_NAMES, login_fail_reason_name


def _logins(client, admin, username):
    rows = client.get("/api/audit/logins", headers=admin, params={"username": username}).json()
    return [(r["fail_reason"], r["fail_reason_name"]) for r in rows]


def test_口令错与账号停用_出参带中文失败原因(client, admin):
    client.post("/api/users", headers=admin, json={
        "username": "p2427_op", "password": "passw0rd1", "role": "operator"})
    assert client.post("/api/auth/login", json={"username": "p2427_op", "password": "wrong-pass"}).status_code == 401
    uid = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "p2427_op")
    client.patch(f"/api/users/{uid}/status", json={"status": "disabled"}, headers=admin)
    assert client.post("/api/auth/login", json={"username": "p2427_op", "password": "passw0rd1"}).status_code == 403
    assert _logins(client, admin, "p2427_op") == [
        ("disabled", "账号已停用"),
        ("bad_credentials", "用户名或密码错误"),   # 修前没有这个键，页面上是 bad_credentials
    ]


def test_成功的那条是空串_表外与现拼的代号():
    assert login_fail_reason_name("") == ""
    assert login_fail_reason_name("code_401") == "短信验证码校验未通过（401）"
    assert login_fail_reason_name("some_future_code") == "some_future_code"   # 表外原样回显，看得见
    assert all(text.strip() for text in LOGIN_FAIL_REASON_NAMES.values())
