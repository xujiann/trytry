"""慢专病团队成员「改角色」弹窗预填常量、恢复在岗不查账号停用（P2-313）。

① 弹窗里患者范围、三个权限位、状态都预填常量（本团队 / 否 / 在岗），与这位成员现在的值无关——只想改个角色、点保存，
   就把他的权限位清空、把停用的人悄悄恢复在岗。
② `PATCH /api/spd/team-members/{id}` 把停用的成员改回在岗时不查账号：加成员时「停用的账号不进成员名单：进了也永远不接活」
   （P1-106）早已挡住，恢复在岗这条路绕过去了。

修法：弹窗各栏按成员现在的值预填；恢复在岗与加成员同一道，账号停用 / 不存在 409。
"""
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2313 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    user = client.post("/api/users", headers=admin, json={
        "username": "p2313_doc", "password": "pw123456", "full_name": "P2313 医生", "role": "doctor", "org_id": org})
    assert user.status_code == 201, user.text
    team = client.post("/api/spd/teams", headers=admin, json={"name": "P2313 团队", "org_id": org, "level": "township"})
    assert team.status_code == 201, team.text
    member = client.post(f"/api/spd/teams/{team.json()['id']}/members", headers=admin,
                         json={"user_id": user.json()["id"], "member_role": "doctor"})
    assert member.status_code == 201, member.text
    return {"user": user.json()["id"], "member": member.json()["id"]}


def test_账号停用的成员不能恢复在岗(client, admin, world):
    url = f"/api/spd/team-members/{world['member']}"
    assert client.patch(url, headers=admin, json={"active": False}).status_code == 200
    assert client.patch(f"/api/users/{world['user']}/status", headers=admin, json={"status": "disabled"}).status_code == 200
    got = client.patch(url, headers=admin, json={"active": True})
    assert got.status_code == 409 and got.json()["detail"] == "成员账号已停用，不能恢复在岗", got.text   # 修前 200
    other = client.patch(url, headers=admin, json={"member_role": "nurse"})   # 不动状态的改档照常
    assert other.status_code == 200 and other.json()["active"] is False, other.text


def test_改角色弹窗按成员现在的值预填_静态钉():
    page = PAGE.read_text(encoding="utf-8")
    for attr in ("data-scope=", "data-referral=", "data-audit=", "data-assess=", "data-active="):
        assert attr in page, attr   # 改角色按钮把成员现值带上
    for field in ("scope", "referral", "audit", "assess", "active"):
        assert f"value: tmEdit.dataset.{field}" in page, field   # 修前是常量 "team" / "0" / "1"
