"""五条登记在案的小缺口一次关账（P1-41 ~ P1-44、P1-47）。

这五条的共同形状是**"做了一半"**：功能建起来了，但缺了让它闭环的那一小块，
而缺的那块往往只在真用起来时才咬人：

* **P1-41** `GET /api/spd/teams` 只回 `active=True`：点一次「停用」团队就从界面上
  消失，**没有任何入口能再启用它**——只能知道 id 直接 PATCH。停用不是删除，
  界面却表现得像删除。
* **P1-42** 医废点位只有 create/list/deactivate/reactivate，**没有更新端点**：
  点位名写错、负责人换人都改不了，只能「停用旧的再建一个」，而那会让历史医废
  指向一个标着「已停用」的点位，查追溯链的人看到的是断掉的线索。
* **P1-43** TOTP **只有写没有读**：能开通、能启用、能解绑，却没有接口回答
  「我到底绑没绑」，界面只能给动作不给回执，更无法提示「你的角色被要求双因素
  但还没绑」。
* **P1-44** `GET /api/access-logs/stats` 的 docstring 写着「一段时间内」，签名里
  却没有时间参数：「上个月的跨机构调阅占比」这类合规排查做不了。
* **P1-47** 医废交接的 `handler_name` 是必填，但挂了 `handler_employee_id` 时
  后端又用 `employee.name` 覆盖它——调用方必须递一个**注定被丢弃**的值。
"""
import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    return {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", json={
        "name": "欠账批次医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin).json()


# ------------------------------------------------------------ P1-41 团队可再启用


def test_停用的团队仍看得见且能再启用(client, admin, org):
    """去掉 include_inactive 分支，本条必红——停用后列表里再也找不到它。"""
    team = client.post("/api/spd/teams", json={
        "name": "欠账批次团队", "org_id": org["id"], "level": "township"},
        headers=admin).json()
    tid = team["id"]

    assert client.patch(f"/api/spd/teams/{tid}", json={"active": False},
                        headers=admin).status_code == 200

    default_ids = [t["id"] for t in client.get("/api/spd/teams?limit=100", headers=admin).json()]
    assert tid not in default_ids, "缺省清单不该带出停用团队（既有口径不变）"

    all_ids = [t["id"] for t in client.get(
        "/api/spd/teams?limit=100&include_inactive=true", headers=admin).json()]
    assert tid in all_ids, "停用后在 include_inactive 清单里也找不到——没有入口能再启用它"

    assert client.patch(f"/api/spd/teams/{tid}", json={"active": True},
                        headers=admin).status_code == 200
    assert tid in [t["id"] for t in client.get("/api/spd/teams?limit=100", headers=admin).json()]


# ------------------------------------------------------------ P1-42 点位可改档


def test_点位可以改名字与负责人(client, admin, org):
    loc = client.post("/api/medwaste/locations", json={
        "org_id": org["id"], "name": "外科处置室", "location_type": "source",
        "manager_name": "老王"}, headers=admin).json()

    resp = client.patch(f"/api/medwaste/locations/{loc['id']}",
                        json={"name": "外科处置室（新）", "manager_name": "小李"},
                        headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "外科处置室（新）"
    assert resp.json()["manager_name"] == "小李"


def test_点位改档改不了归属与类型(client, admin, org):
    """**这是刻意的**：点位换家、产生点改成暂存间，都会让历史医废记录的语义
    被事后改写——那批医废当时是从哪个科室产生、存进哪个暂存间的，不能变。
    未声明的字段被 pydantic 忽略，归属与类型原样不动。"""
    other = client.post("/api/organizations", json={
        "name": "欠账批次别家", "org_type": "township", "level": "township"},
        headers=admin).json()
    loc = client.post("/api/medwaste/locations", json={
        "org_id": org["id"], "name": "处置室甲", "location_type": "source"},
        headers=admin).json()

    client.patch(f"/api/medwaste/locations/{loc['id']}",
                 json={"org_id": other["id"], "location_type": "storage", "name": "改名了"},
                 headers=admin)
    after = [x for x in client.get(
        f"/api/medwaste/locations?org_id={org['id']}", headers=admin).json()
        if x["id"] == loc["id"]]
    assert after, "点位被改到别家去了"
    assert after[0]["location_type"] == "source", "点位类型被改写了"
    assert after[0]["name"] == "改名了", "名字没改成"


def test_点位改档空入参报422(client, admin, org):
    loc = client.post("/api/medwaste/locations", json={
        "org_id": org["id"], "name": "处置室乙", "location_type": "source"},
        headers=admin).json()
    assert client.patch(f"/api/medwaste/locations/{loc['id']}", json={},
                        headers=admin).status_code == 422


# ------------------------------------------------------------ P1-43 TOTP 可读


def test_TOTP有读接口且不回密钥(client, admin):
    """去掉 GET /api/auth/totp，本条必红——此前只有写没有读。"""
    before = client.get("/api/auth/totp", headers=admin)
    assert before.status_code == 200, before.text
    assert before.json()["enabled"] is False
    assert before.json()["pending"] is False

    setup = client.post("/api/auth/totp/setup", headers=admin)
    assert setup.status_code == 200, setup.text

    mid = client.get("/api/auth/totp", headers=admin).json()
    assert mid["pending"] is True and mid["enabled"] is False, "生成密钥后应是 pending 态"
    # 读接口只回状态，密钥只在 setup 那一次返回
    assert "secret" not in mid and "otpauth_uri" not in mid


def test_TOTP读接口回answer角色是否被要求(client, admin, monkeypatch):
    """`action_needed` 是这个读接口最有用的一句：被要求双因素却还没绑。"""
    from app.config import settings

    monkeypatch.setattr(settings, "totp_required_roles", "admin")
    st = client.get("/api/auth/totp", headers=admin).json()
    assert st["required"] is True
    assert st["action_needed"] is (not st["enabled"])


# ------------------------------------------------------------ P1-44 统计带时间窗


def test_调阅统计支持时间窗且回显口径(client, admin, org):
    """去掉 start/end 过滤，本条必红——未来窗口本应把所有记录都排除掉。"""
    patient = client.post("/api/patients", json={
        "name": "欠账批次患者", "id_card": "330103199303031234"}, headers=admin).json()
    # 360 视图的真实路径是 /api/archive/{ehc_no}（encounters 路由的 prefix 是 /api）
    got = client.get(f"/api/archive/{patient['ehc_no']}", headers=admin)
    assert got.status_code == 200, got.text

    full = client.get("/api/access-logs/stats", headers=admin)
    assert full.status_code == 200, full.text
    assert full.json()["total"] >= 1
    assert full.json()["start"] == "" and full.json()["end"] == ""

    empty = client.get("/api/access-logs/stats",
                       params={"start": "2099-01-01", "end": "2099-12-31"}, headers=admin)
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0, "时间窗没生效——未来的窗口不该有任何记录"
    assert empty.json()["start"] == "2099-01-01", "没有回显取数窗口"


def test_调阅统计的时间窗要过日历(client, admin):
    """与平台的 DateStr 同一口径：2026-02-30 不是日期。"""
    assert client.get("/api/access-logs/stats", params={"start": "2026-02-30"},
                      headers=admin).status_code == 422


# ------------------------------------------------------------ P1-47 交接入参二选一


def test_交接挂了档案就不必再递姓名(client, admin, org):
    """去掉 handler_name 的放宽，本条必红——调用方必须递一个注定被丢弃的值。"""
    emp = client.post("/api/mgmt/employees", json={
        "org_id": org["id"], "name": "转运老张", "title": "工勤"}, headers=admin)
    if emp.status_code not in (200, 201):
        pytest.skip(f"员工档案接口不可用：{emp.status_code}")
    waste = client.post("/api/medwaste", json={
        "org_id": org["id"], "waste_type": "sharp", "weight_kg": 0.5,
        "collected_date": "2026-09-19"}, headers=admin).json()

    resp = client.post(f"/api/medwaste/{waste['id']}/handover",
                       json={"handler_employee_id": emp.json()["id"]}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["handler_name"] == "转运老张", "应取档案里的姓名"


def test_交接两个都不给才报错(client, admin, org):
    waste = client.post("/api/medwaste", json={
        "org_id": org["id"], "waste_type": "sharp", "weight_kg": 0.5,
        "collected_date": "2026-09-19"}, headers=admin).json()
    assert client.post(f"/api/medwaste/{waste['id']}/handover", json={},
                       headers=admin).status_code == 422


def test_交接只给姓名照常(client, admin, org):
    """放宽只加宽受理面：既有的「只递姓名」请求必须照常成功。"""
    waste = client.post("/api/medwaste", json={
        "org_id": org["id"], "waste_type": "sharp", "weight_kg": 0.5,
        "collected_date": "2026-09-19"}, headers=admin).json()
    resp = client.post(f"/api/medwaste/{waste['id']}/handover",
                       json={"handler_name": "临时工老李"}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["handler_name"] == "临时工老李"
