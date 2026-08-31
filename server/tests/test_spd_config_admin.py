"""慢专病配置域（`app/spd/routers/config/`）：改与删的分支，以及改完之后会怎样。

这套用例补的是**建之后**的那一半。既有用例把"能不能建出来"覆盖得不错，
但 PATCH / DELETE / 冲突 / 停用几乎没人碰——而实施期真正频繁做的正是这些：
团队换人、村医离职、设备换绑、数据源改频率、量表停用。
它们出错的形态也更隐蔽：建错了当场看得见，改错了要等到下一次业务跑批才发现。

选题按覆盖率缺口排（centers 43% / teams 53% / devices 54% 是最低的三个），
但每条都对着一个真实的运维动作写，不为覆盖率凑数。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    county = client.post(
        "/api/organizations",
        json={"name": "配置域县医院", "org_type": "lead_hospital", "level": "county"},
        headers=h,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "配置域卫生院", "org_type": "township", "level": "township",
              "parent_id": county["id"]},
        headers=h,
    ).json()
    village = client.post(
        "/api/organizations",
        json={"name": "配置域村卫生室", "org_type": "village", "level": "village",
              "parent_id": township["id"]},
        headers=h,
    ).json()
    doctor = client.post(
        "/api/users",
        json={"username": "cfg_doctor", "password": "passw0rd1", "role": "doctor",
              "full_name": "配置域医生", "org_id": township["id"]},
        headers=h,
    ).json()
    nurse = client.post(
        "/api/users",
        json={"username": "cfg_nurse", "password": "passw0rd1", "role": "doctor",
              "full_name": "配置域护士", "org_id": township["id"]},
        headers=h,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "配置域患者", "id_card": "330377199009090055", "gender": "男",
              "birth_date": "1990-09-09"},
        headers=h,
    ).json()
    return {"county": county, "township": township, "village": village,
            "doctor": doctor, "nurse": nurse, "patient": patient}


# ============================================================ 专病中心


def test_中心编码重复被拦下而不是建出两个(client, h, base):
    body = {"code": "ctr_hyp", "name": "高血压中心", "program_code": "hypertension",
            "lead_org_id": base["county"]["id"], "lead_dept": "心内科"}
    first = client.post("/api/spd/centers", json=body, headers=h)
    assert first.status_code == 201, first.text
    assert client.post("/api/spd/centers", json=body, headers=h).status_code == 409


def test_中心绑定不存在的病种应当404(client, h):
    resp = client.post(
        "/api/spd/centers",
        json={"code": "ctr_ghost", "name": "幽灵中心", "program_code": "no_such_program"},
        headers=h,
    )
    assert resp.status_code == 404


def test_中心改牵头科室与停用(client, h, base):
    center = client.post(
        "/api/spd/centers",
        json={"code": "ctr_dm", "name": "糖尿病中心", "program_code": "diabetes",
              "lead_org_id": base["county"]["id"]},
        headers=h,
    ).json()
    updated = client.patch(
        f"/api/spd/centers/{center['id']}",
        json={"lead_dept": "内分泌科", "status": "disabled",
              "org_ids": [base["township"]["id"]]},
        headers=h,
    ).json()
    assert updated["lead_dept"] == "内分泌科"
    assert updated["status"] == "disabled"
    assert updated["org_ids"] == [base["township"]["id"]]
    assert client.patch("/api/spd/centers/999999", json={"name": "不存在"},
                        headers=h).status_code == 404

    listed = client.get("/api/spd/centers?program_code=diabetes", headers=h).json()
    assert [c["code"] for c in listed] == ["ctr_dm"], "按病种过滤应只返回该病种的中心"


def test_机构树带团队数与在管数(client, h, base):
    """配置的人要判断"改这个机构会影响到谁"，光有机构名不够。"""
    tree = client.get("/api/spd/org-tree", headers=h).json()
    flat = {}

    def walk(nodes):
        for node in nodes:
            flat[node["id"]] = node
            walk(node.get("children") or [])

    walk(tree if isinstance(tree, list) else tree.get("nodes", []))
    assert base["county"]["id"] in flat
    node = flat[base["county"]["id"]]
    assert {"team_count", "enrolled", "children"} <= set(node)
    child_ids = {c["id"] for c in node["children"]}
    assert base["township"]["id"] in child_ids, "乡镇应挂在县院下面"


# ============================================================ 服务团队与成员


def test_团队成员增改删走完整生命周期(client, h, base):
    team = client.post(
        "/api/spd/teams",
        json={"name": "配置域高血压团队", "org_id": base["township"]["id"],
              "level": "township", "program_codes": ["hypertension"]},
        headers=h,
    ).json()
    member = client.post(
        f"/api/spd/teams/{team['id']}/members",
        json={"user_id": base["doctor"]["id"], "member_role": "doctor",
              "can_referral": True},
        headers=h,
    )
    assert member.status_code == 201, member.text
    member = member.json()

    detail = client.get(f"/api/spd/teams/{team['id']}", headers=h).json()
    assert [m["user_id"] for m in detail["members"]] == [base["doctor"]["id"]]

    # 改权限：能转诊的人换成不能转诊，加上审核权。
    # 断言走团队详情而不是 PATCH 的响应体——那个响应只回 id/角色/启用三项，
    # 权限有没有真的落库，只有再查一次才算数
    assert client.patch(
        f"/api/spd/team-members/{member['id']}",
        json={"can_referral": False, "can_audit": True, "patient_scope": "org"},
        headers=h,
    ).status_code == 200
    changed = next(
        m for m in client.get(f"/api/spd/teams/{team['id']}", headers=h).json()["members"]
        if m["id"] == member["id"]
    )
    assert changed["can_referral"] is False and changed["can_audit"] is True
    assert changed["patient_scope"] == "org"

    # 移出团队后，团队详情里不该还挂着这个人
    assert client.delete(f"/api/spd/team-members/{member['id']}",
                         headers=h).status_code == 204
    detail = client.get(f"/api/spd/teams/{team['id']}", headers=h).json()
    assert detail["members"] == []
    assert client.delete(f"/api/spd/team-members/{member['id']}",
                         headers=h).status_code == 404


def test_同一个人不重复加入同一团队(client, h, base):
    team = client.post(
        "/api/spd/teams",
        json={"name": "配置域糖尿病团队", "org_id": base["township"]["id"],
              "program_codes": ["diabetes"]},
        headers=h,
    ).json()
    body = {"user_id": base["nurse"]["id"], "member_role": "nurse"}
    assert client.post(f"/api/spd/teams/{team['id']}/members", json=body,
                       headers=h).status_code == 201
    again = client.post(f"/api/spd/teams/{team['id']}/members", json=body, headers=h)
    assert again.status_code == 409, "同一个人加两次会让待办分给同一人两份"


def test_团队改名与停用后清单可筛(client, h, base):
    team = client.post(
        "/api/spd/teams",
        json={"name": "待改名团队", "org_id": base["village"]["id"], "level": "village"},
        headers=h,
    ).json()
    updated = client.patch(
        f"/api/spd/teams/{team['id']}",
        json={"name": "已改名团队", "active": False, "service_area": "杨庄片区"},
        headers=h,
    ).json()
    assert updated["name"] == "已改名团队" and updated["active"] is False
    active_only = client.get("/api/spd/teams?active=true", headers=h).json()
    assert team["id"] not in [t["id"] for t in active_only], "停用团队不该出现在启用清单里"


# ============================================================ 村医档案


def test_村医批量导入跳过重复并逐条报结果(client, h, base):
    """批量导入最怕"报了成功、实际没进"，所以逐条给结果。"""
    resp = client.post(
        "/api/spd/village-doctors/batch",
        json={"items": [
            {"user_id": base["doctor"]["id"], "org_id": base["village"]["id"],
             "township": "配置镇", "village": "杨庄村"},
            {"user_id": base["nurse"]["id"], "org_id": base["village"]["id"],
             "township": "配置镇", "village": "李庄村"},
        ]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["created"] == 2

    # 同一个人再导一次：不该建出第二份档案
    again = client.post(
        "/api/spd/village-doctors/batch",
        json={"items": [{"user_id": base["doctor"]["id"], "org_id": base["village"]["id"],
                         "township": "配置镇", "village": "杨庄村"}]},
        headers=h,
    ).json()
    assert again["created"] == 0, "同一个 user_id 只该有一份村医档案"

    listed = client.get(
        f"/api/spd/village-doctors?org_id={base['village']['id']}", headers=h
    ).json()
    assert len(listed) == 2


def test_村医停用后绑定码不再出(client, h, base):
    """码是入口，入口先于账号被回收。"""
    vd = client.get(f"/api/spd/village-doctors?org_id={base['village']['id']}",
                    headers=h).json()[0]
    assert client.get(f"/api/spd/village-doctors/{vd['id']}/qr.svg",
                      headers=h).status_code == 200
    client.patch(f"/api/spd/village-doctors/{vd['id']}", json={"active": False}, headers=h)
    assert client.get(f"/api/spd/village-doctors/{vd['id']}/qr.svg",
                      headers=h).status_code == 409
    client.patch(f"/api/spd/village-doctors/{vd['id']}", json={"active": True}, headers=h)


# ============================================================ 设备与数据源


def test_设备绑定与解绑走同一个接口(client, h, base):
    device = client.post(
        "/api/spd/devices",
        json={"sn": "BP-CFG-001", "device_type": "bp", "model": "欧姆龙U30",
              "org_id": base["township"]["id"]},
        headers=h,
    ).json()
    assert device["status"] == "idle"

    bound = client.post(
        f"/api/spd/devices/{device['id']}/bind",
        json={"patient_id": base["patient"]["id"]}, headers=h,
    ).json()
    assert bound["status"] == "bound" and bound["bound_patient_id"] == base["patient"]["id"]

    unbound = client.post(
        f"/api/spd/devices/{device['id']}/bind", json={"patient_id": None}, headers=h,
    ).json()
    assert unbound["status"] == "idle" and unbound["bound_patient_id"] is None
    assert client.post("/api/spd/devices/999999/bind", json={"patient_id": None},
                       headers=h).status_code == 404


def test_设备序列号唯一(client, h, base):
    body = {"sn": "BP-CFG-DUP", "device_type": "bp", "org_id": base["township"]["id"]}
    assert client.post("/api/spd/devices", json=body, headers=h).status_code == 201
    assert client.post("/api/spd/devices", json=body, headers=h).status_code == 409


def test_数据源改频率与停用会反映到监控(client, h, base):
    source = client.post(
        "/api/spd/data-sources",
        json={"code": "cfg_ph", "name": "配置域公卫库", "source_type": "publichealth",
              "freq_minutes": 60},
        headers=h,
    ).json()
    updated = client.patch(
        f"/api/spd/data-sources/{source['id']}",
        json={"freq_minutes": 5, "endpoint": "http://his.local/api"},
        headers=h,
    ).json()
    assert updated["freq_minutes"] == 5 and updated["endpoint"] == "http://his.local/api"

    # 人工回填一次同步日志（外部系统推过来的口径），监控随之更新
    client.post(
        f"/api/spd/data-sources/{source['id']}/sync-logs",
        json={"rows": 120, "latency_ms": 800, "success": True, "message": "全量"},
        headers=h,
    )
    client.post(
        f"/api/spd/data-sources/{source['id']}/sync-logs",
        json={"rows": 0, "latency_ms": 50, "success": False, "message": "对端超时"},
        headers=h,
    )
    logs = client.get(f"/api/spd/data-sources/{source['id']}/sync-logs", headers=h).json()
    assert [log["success"] for log in logs[:2]] == [False, True], "最近的在前"

    monitor = client.get("/api/spd/data-sources-monitor", headers=h).json()
    assert monitor["total"] >= 1
    mine = next(s for s in client.get("/api/spd/data-sources", headers=h).json()
                if s["id"] == source["id"])
    assert mine["success_rate"] == 50.0, "两次一成一败，成功率就是 50%"
    assert mine["status"] == "failed", "最近一次失败，状态要如实是失败"

    client.patch(f"/api/spd/data-sources/{source['id']}", json={"active": False}, headers=h)
    assert client.patch("/api/spd/data-sources/999999", json={"active": False},
                        headers=h).status_code == 404
