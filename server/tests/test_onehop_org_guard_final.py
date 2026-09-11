"""归属隔一跳的最后五个写端点（2026-09-11 实测取证后补，一跳欠账清零）。

## 实测取证（乙院，对象全在甲院名下）

    PATCH  /api/spd/team-members/{id}         → 200  把甲院团队成员改名、停用
    DELETE /api/spd/team-members/{id}         → 204  直接删掉
    POST   /api/spd/qc-samples/{id}/result    → 200  给甲院的抽查样本写"不合格"
    POST   /api/appointments/{id}/cancel      → 200  取消甲院号源上的预约
    POST   /api/appointments/{id}/fulfill     → 200  替甲院核销到诊

五个端点原先**都没有 `user` 形参**。

## 三处归属各自隔着一跳

    appointments        slot_id    → appointment_slots.org_id
    spd_team_members    team_id    → spd_teams.org_id
    spd_qc_samples      record_id  → spd_followup_records.org_id

## 每一处的理由不一样，所以逐条写

- **团队成员**：同文件的 `update_team` / `add_team_member` 一直
  `assert_org_writable`（本文件共 7 处），只有成员改/删两个没跟上——
  又是"同一个文件、两套口径"。
- **预约**：守卫写在端点里而**不是** `release_appointment` 里。那个函数是
  管理端与居民端共用的（`portal.py` 也调它），居民取消自己的预约走门户令牌
  那套口径，不该被员工的机构规则判。
- **质控抽查**：这不是给质控加新口径，是把同一个功能的两半对齐。
  同文件 `plan_qc`（生成抽查计划）一直按 `visible_org_ids` 收口——抽得到谁，
  本来就只有可见范围内那些；而判结论这一半什么都不校验。
  合格率是考核依据，能被无关机构写进去，这个数就不能用了。
"""
import pytest

from app import models as M
from app.database import SessionLocal


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "末批甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "末批乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("fin_op_a", a, "operator"), ("fin_doc_a", a, "doctor"),
                             ("fin_op_b", b, "operator"), ("fin_doc_b", b, "doctor"),
                             ("fin_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    patients = [
        client.post("/api/patients",
                    json={"name": f"末批患者{n}", "id_card": f"33078219950505{n:04d}"},
                    headers=admin).json()
        for n in (1, 2, 3, 4)
    ]
    return {"admin": admin, "a": a, "b": b, "patients": patients,
            "op_a": _login(client, "fin_op_a"), "doc_a": _login(client, "fin_doc_a"),
            "op_b": _login(client, "fin_op_b"), "doc_b": _login(client, "fin_doc_b"),
            "dir_b": _login(client, "fin_dir_b")}


_SEQ = iter(range(1, 999))


def _seed(world):
    """直接落库造出"甲院名下"的三类对象（建它们的流程不是本文件要证的东西）。"""
    db = SessionLocal()
    try:
        n = next(_SEQ)
        org_id = world["a"]["id"]
        team = M.SpdTeam(name=f"甲院团队{n}", org_id=org_id)
        slot = M.AppointmentSlot(org_id=org_id, resource_type="doctor",
                                 resource_name=f"甲院内科{n}", slot_date="2026-12-01")
        record = M.SpdFollowupRecord(patient_id=world["patients"][0]["id"], org_id=org_id)
        db.add_all([team, slot, record])
        db.flush()
        member = M.SpdTeamMember(team_id=team.id, user_id=1)
        # 同一号源下同一患者唯一，故两条预约用两位患者
        appt_a = M.Appointment(slot_id=slot.id, patient_id=world["patients"][1]["id"],
                               status="booked")
        appt_b = M.Appointment(slot_id=slot.id, patient_id=world["patients"][2]["id"],
                               status="booked")
        sample = M.SpdQcSample(record_id=record.id, batch=f"QC{n}")
        db.add_all([member, appt_a, appt_b, sample])
        db.commit()
        return {"member": member.id, "appt_a": appt_a.id, "appt_b": appt_b.id,
                "sample": sample.id}
    finally:
        db.close()


# ------------------------------------------------ 一个方向：无关机构一律 403


def test_别家机构改不了团队成员(client, world):
    ids = _seed(world)
    resp = client.patch(f"/api/spd/team-members/{ids['member']}",
                        json={"member_role": "乙院改的", "active": False},
                        headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构删不了团队成员(client, world):
    ids = _seed(world)
    assert client.delete(f"/api/spd/team-members/{ids['member']}",
                         headers=world["doc_b"]).status_code == 403


def test_别家机构写不了质控结论(client, world):
    """合格率是考核依据——能被无关机构写进去，这个数就不能用了。"""
    ids = _seed(world)
    resp = client.post(f"/api/spd/qc-samples/{ids['sample']}/result",
                       json={"result": "fail", "method": "phone", "note": "乙院判的"},
                       headers=world["doc_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构取消不了预约(client, world):
    ids = _seed(world)
    assert client.post(f"/api/appointments/{ids['appt_a']}/cancel",
                       headers=world["op_b"]).status_code == 403


def test_别家机构核销不了到诊(client, world):
    ids = _seed(world)
    assert client.post(f"/api/appointments/{ids['appt_b']}/fulfill",
                       headers=world["op_b"]).status_code == 403


# ------------------------------------------------ 另一个方向：不能变成"全关了"


def test_本机构照常(client, world):
    ids = _seed(world)
    assert client.patch(f"/api/spd/team-members/{ids['member']}",
                        json={"member_role": "组长"}, headers=world["doc_a"]).status_code == 200
    assert client.post(f"/api/spd/qc-samples/{ids['sample']}/result",
                       json={"result": "pass", "method": "record"},
                       headers=world["doc_a"]).status_code == 200
    assert client.post(f"/api/appointments/{ids['appt_a']}/fulfill",
                       headers=world["op_a"]).status_code == 200
    assert client.post(f"/api/appointments/{ids['appt_b']}/cancel",
                       headers=world["op_a"]).status_code == 200
    assert client.delete(f"/api/spd/team-members/{ids['member']}",
                         headers=world["doc_a"]).status_code == 204


def test_全域角色跨机构照常(client, world):
    """县级中心跨机构判质控、调团队，是质控与统筹的本意。"""
    ids = _seed(world)
    assert client.post(f"/api/spd/qc-samples/{ids['sample']}/result",
                       json={"result": "warn", "method": "wechat"},
                       headers=world["dir_b"]).status_code == 200
    assert client.patch(f"/api/spd/team-members/{ids['member']}",
                        json={"active": False}, headers=world["dir_b"]).status_code == 200


# ------------------------------------------------ 顺序与范围


def test_对别家先403不泄露状态(client, world):
    """归属判定排在状态机之前：别家应当看到 403，而不是"当前状态 X 不可核销"。"""
    ids = _seed(world)
    assert client.post(f"/api/appointments/{ids['appt_a']}/fulfill",
                       headers=world["op_a"]).status_code == 200
    again = client.post(f"/api/appointments/{ids['appt_a']}/fulfill", headers=world["op_b"])
    assert again.status_code == 403, "本院重复核销才该是 409，别家机构应当先被 403 挡下"


def test_居民端取消未被员工机构规则波及(client, world):
    """守卫写在端点里而不是共用的 `release_appointment` 里——这条钉住那个决定。

    `portal.py` 的居民取消走同一个函数。把守卫塞进那个函数，居民（没有 `org_id`
    的门户身份）取消自己的预约就会被员工的机构规则判，那是另一套口径。
    这里直接验证该函数自身不含机构判定。
    """
    import inspect

    from app.routers.appointments import release_appointment

    src = inspect.getsource(release_appointment)
    assert "assert_org_writable" not in src, (
        "机构守卫不该下沉进 release_appointment——它与居民端共用"
    )
