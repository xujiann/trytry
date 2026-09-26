"""病种启停是平台级开关：原先任一机构的医师一句 PATCH 就能把全县的某个病种停掉（P1-169）。

配置包的说明写「配置写接口统一收在 require_roles("director")……真正的平台级开关（病种启停、数据源接入）仍然要 admin」，
建病种（`POST /api/spd/programs`）确实是 `require_admin`；改档（`PATCH /api/spd/programs/{id}`）却按 `CONFIG_ROLES`
（主任、医师）放行、`active` 不另判。修前实测：村卫生室的医生 `{"active": false}` 200——此后全县这个病种的筛查登记、
目标池扫描、建档一律 404「专病档案不存在或已停用」，就诊触发的自动识别也跳过它。

修法：真的改了启停的，只许平台管理员；其余字段照旧按 CONFIG_ROLES（界面上的「编辑病种」不送 `active`，不受影响）。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1169 村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    for username, role in (("p1169_doc", "doctor"), ("p1169_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": role, "org_id": org})
        assert created.status_code == 201, created.text
    program = client.post(f"{B}/programs", headers=admin, json={"code": "P1169_DM", "name": "P1169 糖尿病"})
    assert program.status_code == 201, program.text
    return {"program": program.json()["id"], "doc": _login(client, "p1169_doc"), "dir": _login(client, "p1169_dir")}


def _active(program_id):
    from app.spd.models import SpdProgram

    with SessionLocal() as db:
        return db.get(SpdProgram, program_id).active


@pytest.mark.parametrize("who", ["doc", "dir"])
def test_非管理员停用病种_403(client, world, who):
    got = client.patch(f"{B}/programs/{world['program']}", headers=world[who], json={"active": False})
    assert got.status_code == 403, got.text   # 修前 200（医师、主任都能停全县的病种）
    assert _active(world["program"]) is True


def test_非管理员照常改其余字段_带着没变的启停也不拦(client, world):
    got = client.patch(f"{B}/programs/{world['program']}", headers=world["doc"],
                       json={"lead_dept": "内分泌科", "active": True})
    assert got.status_code == 200, got.text
    assert got.json()["lead_dept"] == "内分泌科"


def test_管理员照常启停(client, admin, world):
    off = client.patch(f"{B}/programs/{world['program']}", headers=admin, json={"active": False})
    assert off.status_code == 200 and off.json()["active"] is False, off.text
    on = client.patch(f"{B}/programs/{world['program']}", headers=admin, json={"active": True})
    assert on.status_code == 200 and on.json()["active"] is True, on.text
