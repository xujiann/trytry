"""平台侧转诊状态推进的机构归属校验（口径裁定 1 的前置）。

`PATCH /api/referrals/{id}/status` 此前只有 `require_roles("doctor")`——任何机构的
任何医师都能把别人的单子接诊掉、结案掉。实测确认过（本文件第一条就是那个回归）。

为什么这条比"分母该算谁的"更要紧：`completed` 是「转诊结案率」的**分子**，
该指标进 `performance/orgs` 的绩效评分，绩效评分又被 `fund.distribute` 用来
**切分基金池**。分子谁都能改，讨论分母口径没有意义。

规则：只有接收方机构（`to_org_id`）能推进；全域角色（admin/director）放行——
与 spd 侧 `_assert_holds_case` 同一套兜底逻辑（中心代录场景）。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name):
        return client.post(
            "/api/organizations",
            json={"name": name, "org_type": "township", "level": "township"},
            headers=admin,
        ).json()

    def doctor(username, org_id):
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "role": "doctor",
                  "org_id": org_id},
            headers=admin,
        )
        r = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    sender, receiver, stranger = org("转诊转出院"), org("转诊接收院"), org("转诊无关院")
    patient = client.post(
        "/api/patients", json={"name": "转诊权限患者", "id_card": "330281199002021230"},
        headers=admin,
    ).json()
    return {
        "sender": sender, "receiver": receiver, "stranger": stranger, "patient": patient,
        "doc_sender": doctor("ref_doc_send", sender["id"]),
        "doc_receiver": doctor("ref_doc_recv", receiver["id"]),
        "doc_stranger": doctor("ref_doc_other", stranger["id"]),
    }


def _new_referral(client, admin, world):
    return client.post(
        "/api/referrals",
        json={"patient_id": world["patient"]["id"], "from_org_id": world["sender"]["id"],
              "to_org_id": world["receiver"]["id"], "direction": "up", "reason": "上转"},
        headers=admin,
    ).json()


def test_无关机构的医师不得接诊别人的单子(client, admin, world):
    """这条就是那个存量漏洞的回归：改之前它返回 200。"""
    ref = _new_referral(client, admin, world)
    resp = client.patch(f"/api/referrals/{ref['id']}/status",
                        json={"status": "accepted"}, headers=world["doc_stranger"])
    assert resp.status_code == 403, resp.text
    assert "接收机构" in resp.json()["detail"]


def test_无关机构的医师不得结案别人的单子(client, admin, world):
    """`completed` 是绩效结案率的分子，最终影响基金分配——这条守的是钱。"""
    ref = _new_referral(client, admin, world)
    client.patch(f"/api/referrals/{ref['id']}/status",
                 json={"status": "accepted"}, headers=world["doc_receiver"])
    resp = client.patch(f"/api/referrals/{ref['id']}/status",
                        json={"status": "completed"}, headers=world["doc_stranger"])
    assert resp.status_code == 403, resp.text
    got = client.get(f"/api/referrals/{ref['id']}", headers=admin)
    if got.status_code == 200:
        assert got.json()["status"] == "accepted", "被拒的调用却改掉了状态"


def test_转出方也不能替接收方接诊(client, admin, world):
    """转出方是利益相关方：能自己把自己转出去的单子结案，结案率就成了自评。"""
    ref = _new_referral(client, admin, world)
    resp = client.patch(f"/api/referrals/{ref['id']}/status",
                        json={"status": "accepted"}, headers=world["doc_sender"])
    assert resp.status_code == 403, resp.text


def test_接收方可以正常走完全流程(client, admin, world):
    """校验不能把正路堵死。"""
    ref = _new_referral(client, admin, world)
    for status in ("accepted", "completed"):
        resp = client.patch(f"/api/referrals/{ref['id']}/status",
                            json={"status": status}, headers=world["doc_receiver"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == status


def test_全域角色放行_中心代录(client, admin, world):
    """admin/director 的 org_id 常为空，中心代录场景由它们兜底（与 spd 侧同源）。"""
    ref = _new_referral(client, admin, world)
    assert client.patch(f"/api/referrals/{ref['id']}/status",
                        json={"status": "accepted"}, headers=admin).status_code == 200


def test_归属校验先于状态机_不泄露单据当前状态(client, admin, world):
    """无关机构对一个**状态不对**的单子发请求，应当得到 403 而不是 409。

    先判状态机就等于告诉外人"这张单现在不是 pending"——归属都没有的人
    不该从错误码里读出单据状态。
    """
    ref = _new_referral(client, admin, world)
    client.patch(f"/api/referrals/{ref['id']}/status",
                 json={"status": "accepted"}, headers=world["doc_receiver"])
    resp = client.patch(f"/api/referrals/{ref['id']}/status",
                        json={"status": "accepted"}, headers=world["doc_stranger"])
    assert resp.status_code == 403, f"应是 403（无归属），实为 {resp.status_code}"
