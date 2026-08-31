"""慢专病转诊域（`spd/referral`）14 个端点的**响应契约**特征化网。

场景全部**经 HTTP API 种出来**：真实三级机构树（村→乡→县）+ 三个机构医生账号，
沿 ADR-0004/0005 的分级审核链把一张单推到闭环——契约治理只加出参声明，
审核语义一行不动，这里的用例就是"没动"的证据（谁能推、推到哪全按现状钉死）。
对代表性端点断言完整精确 JSON（dict 相等）与键序（`list(resp.json().keys())`）。

三处最要紧的判断（加 `response_model` 前后都得成立）：

1. **`steps` 是详情端点独有的键**：`GET /referrals/{id}` 恒带 `steps`（在末尾），
   其余 8 个出转诊单的端点从不带——是两个模型（`…DetailOut` 继承追加），
   不是同一个模型上的可空 `steps`（那会给列表行注入 `"steps": null`）。
2. **`closed_at` 未闭环是空串不是 null**（`isoformat() if ... else ""`），
   在途/闭环/撤回/退回四种取值都有用例钉住。
3. **数值只有 Integer 裸 int 与 `round(x/y*100, 1)` 派生 float**
   （closure_rate/effective_rate，空分母也是 `0.0`）；`facts` 里的监测值来自
   Float 列（`bp_sys` 存 170 读回 `170.0`）。
"""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/spd"
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

RULE_KEYS = ["id", "code", "name", "program_code", "scene", "conditions", "notify_role",
             "handle_level", "target_org_id", "auto_task", "active"]
CASE_KEYS = ["id", "patient_id", "patient_name", "program_code", "enrollment_id",
             "direction", "initiator_org_id", "initiator_id", "current_org_id",
             "current_level", "target_org_id", "status", "reason", "trigger_rule_code",
             "trigger_evidence", "materials", "effective_visit", "stable_for_down",
             "created_at", "closed_at"]
STEP_KEYS = ["id", "step", "action", "actor_id", "org_id", "opinion", "created_at"]
COND = {"field": "bp_sys", "op": ">=", "value": 160, "label": "收缩压红线"}


def _ts(value):
    """时间戳只钉格式不钉取值——它是本场景里唯一不可复现的字节。"""
    assert isinstance(value, str) and ISO_TS.match(value), value
    return value


@pytest.fixture(scope="module")
def world(client, auth):
    """村→乡→县三级机构 + 三个机构医生 + 一名有村级就诊史的患者。

    五张转诊单：闭环链（case1）、撤回（case2）、退回（case3）、规则自动开单
    （case_auto，停在 submitted）、停在 accepted 供 404 目标机构分支用（case4）。
    """
    county = client.post(
        "/api/organizations",
        json={"name": "契约县医院", "org_type": "lead_hospital", "level": "county"},
        headers=auth,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "契约卫生院", "org_type": "township", "level": "township",
              "parent_id": county["id"]},
        headers=auth,
    ).json()
    village = client.post(
        "/api/organizations",
        json={"name": "契约村卫生室", "org_type": "village", "level": "village",
              "parent_id": township["id"]},
        headers=auth,
    ).json()

    def _doctor(username, org_id):
        created = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": "doctor",
                  "org_id": org_id},
            headers=auth,
        ).json()
        login = client.post("/api/auth/login",
                            json={"username": username, "password": "pass123456"}).json()
        return created["id"], {"Authorization": f"Bearer {login['access_token']}"}

    vdoc_id, vdoc = _doctor("ct_ref_vdoc", village["id"])
    tdoc_id, tdoc = _doctor("ct_ref_tdoc", township["id"])
    cdoc_id, cdoc = _doctor("ct_ref_cdoc", county["id"])

    patient = client.post(
        "/api/patients",
        json={"name": "契约转诊患者", "id_card": "330666199006150033", "gender": "男",
              "birth_date": "1990-06-15", "phone": "13900003333"},
        headers=auth,
    ).json()
    # 村级就诊史 = 村医的调阅依据（visibility），不带诊断让 facts 保持可精确断言
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": village["id"],
              "encounter_type": "outpatient"},
        headers=auth,
    )
    # 最近一次收缩压 170（Float 列，读回 170.0）
    measured = client.post(
        f"{B}/measurements",
        json={"patient_id": patient["id"], "metric": "bp_sys", "value": 170,
              "unit": "mmHg"},
        headers=auth,
    )
    assert measured.status_code == 201, measured.text

    rule = client.post(
        f"{B}/referral-rules",
        json={"code": "ct_ref_rule", "name": "血压红线上转", "scene": "followup",
              "conditions": [COND], "notify_role": "doctor",
              "handle_level": "township", "target_org_id": county["id"]},
        headers=auth,
    )
    assert rule.status_code == 201, rule.text
    # 走一遍"改 conditions 要重新校验"的分支（同值回写，规则字节不变）
    rule_patched = client.patch(
        f"{B}/referral-rules/{rule.json()['id']}",
        json={"conditions": [COND]}, headers=auth,
    )
    assert rule_patched.status_code == 200, rule_patched.text

    check = client.post(
        f"{B}/referral-rules/check",
        json={"patient_id": patient["id"]}, headers=vdoc,
    )
    assert check.status_code == 200, check.text
    check_auto = client.post(
        f"{B}/referral-rules/check",
        json={"patient_id": patient["id"], "auto_create": True}, headers=vdoc,
    )
    assert check_auto.status_code == 200, check_auto.text

    def _case(reason, **extra):
        resp = client.post(
            f"{B}/referrals",
            json={"patient_id": patient["id"], "direction": "up",
                  "target_org_id": county["id"], "reason": reason, **extra},
            headers=vdoc,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    case1 = _case("血压持续不达标",
                  materials=[{"name": "近3月血压记录", "url": "http://cdn/bp.pdf"}])
    cid = case1["id"]
    review1 = client.post(f"{B}/referrals/{cid}/review",
                          json={"action": "pass", "opinion": "同意上转"}, headers=tdoc)
    assert review1.status_code == 200, review1.text
    review2 = client.post(f"{B}/referrals/{cid}/review",
                          json={"action": "pass", "opinion": "接收住院"}, headers=cdoc)
    assert review2.status_code == 200, review2.text
    arrived = client.post(f"{B}/referrals/{cid}/arrive",
                          json={"effective_visit": True, "opinion": "已到院"},
                          headers=cdoc)
    assert arrived.status_code == 200, arrived.text
    down = client.post(
        f"{B}/referrals/{cid}/down",
        json={"target_org_id": township["id"], "stable": True, "opinion": "情况稳定下转"},
        headers=cdoc,
    )
    assert down.status_code == 200, down.text
    closed = client.post(f"{B}/referrals/{cid}/receive-followup",
                         json={"opinion": "已接收随访"}, headers=tdoc)
    assert closed.status_code == 200, closed.text

    case2 = _case("误开撤回")
    withdrawn = client.post(f"{B}/referrals/{case2['id']}/withdraw", headers=vdoc)
    assert withdrawn.status_code == 200, withdrawn.text
    case3 = _case("资料待补")
    rejected = client.post(f"{B}/referrals/{case3['id']}/review",
                           json={"action": "reject", "opinion": "资料不全退回"},
                           headers=tdoc)
    assert rejected.status_code == 200, rejected.text
    case4 = _case("留在已接收态")
    for headers in (tdoc, cdoc):
        resp = client.post(f"{B}/referrals/{case4['id']}/review",
                           json={"action": "pass"}, headers=headers)
        assert resp.status_code == 200, resp.text

    return {
        "county": county, "township": township, "village": village,
        "vdoc_id": vdoc_id, "tdoc_id": tdoc_id, "cdoc_id": cdoc_id,
        "vdoc": vdoc, "tdoc": tdoc, "cdoc": cdoc, "patient": patient,
        "rule": rule.json(), "rule_keys": list(rule.json().keys()),
        "rule_patched": rule_patched.json(),
        "check": check.json(), "check_keys": list(check.json().keys()),
        "check_auto": check_auto.json(),
        "case1": case1, "case1_keys": list(case1.keys()),
        "review1": review1.json(), "review2": review2.json(),
        "arrived": arrived.json(), "down": down.json(), "closed": closed.json(),
        "case2": case2, "withdrawn": withdrawn.json(),
        "case3": case3, "rejected": rejected.json(), "case4": case4,
    }


def _case1(world, **overrides):
    """按当前世界推出闭环链那张单的完整精确出参（created_at 由调用处代入）。"""
    base = {
        "id": world["case1"]["id"], "patient_id": world["patient"]["id"],
        "patient_name": "契约转诊患者", "program_code": "", "enrollment_id": None,
        "direction": "up", "initiator_org_id": world["village"]["id"],
        "initiator_id": world["vdoc_id"], "current_org_id": world["village"]["id"],
        "current_level": "village", "target_org_id": world["county"]["id"],
        "status": "submitted", "reason": "血压持续不达标", "trigger_rule_code": "",
        "trigger_evidence": {},
        "materials": [{"name": "近3月血压记录", "url": "http://cdn/bp.pdf"}],
        "effective_visit": False, "stable_for_down": False, "closed_at": "",
    }
    base.update(overrides)
    return base


# ============================================================ 转诊规则


def test_规则新建回执完整精确(client, auth, world):
    created = world["rule"]
    assert world["rule_keys"] == RULE_KEYS
    assert created == {
        "id": created["id"], "code": "ct_ref_rule", "name": "血压红线上转",
        "program_code": "", "scene": "followup", "conditions": [COND],
        "notify_role": "doctor", "handle_level": "township",
        "target_org_id": world["county"]["id"], "auto_task": True, "active": True,
    }
    assert list(created["conditions"][0].keys()) == ["field", "op", "value", "label"]


def test_规则列表行与修改回执同形(client, auth, world):
    rows = client.get(f"{B}/referral-rules", headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_ref_rule")
    assert list(mine.keys()) == RULE_KEYS
    assert mine == world["rule"]
    # 同值回写 conditions（走重新校验分支），回执与新建逐字节一致
    assert world["rule_patched"] == world["rule"]


def test_规则试算_触发未开单分支(client, auth, world):
    body = world["check"]
    assert world["check_keys"] == ["triggered", "hits", "facts", "case"]
    today, born = date.today(), date(1990, 6, 15)
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    assert body == {
        "triggered": True,
        "hits": [{"rule": world["rule"], "matched": [COND]}],
        # bp_sys 是 Float 列：存 170 读回 170.0
        "facts": {"age": age, "gender": "男", "diagnosis": [], "diagnosis_name": [],
                  "bp_sys": 170.0},
        "case": None,  # auto_create=false：是否真的转留给医生点
    }
    assert list(body["hits"][0].keys()) == ["rule", "matched"]
    assert isinstance(body["facts"]["bp_sys"], float)


def test_规则试算_自动开单分支(client, auth, world):
    body = world["check_auto"]
    case = body["case"]
    assert list(case.keys()) == CASE_KEYS
    assert case == {
        "id": case["id"], "patient_id": world["patient"]["id"],
        "patient_name": "契约转诊患者", "program_code": "", "enrollment_id": None,
        "direction": "up", "initiator_org_id": world["village"]["id"],
        "initiator_id": world["vdoc_id"], "current_org_id": world["village"]["id"],
        "current_level": "village", "target_org_id": world["county"]["id"],
        "status": "submitted", "reason": "血压红线上转",
        "trigger_rule_code": "ct_ref_rule", "trigger_evidence": {"matched": [COND]},
        "materials": [], "effective_visit": False, "stable_for_down": False,
        "created_at": _ts(case["created_at"]), "closed_at": "",
    }


# ============================================================ 转诊单与分级链路


def test_发起回执完整精确(client, auth, world):
    assert world["case1_keys"] == CASE_KEYS
    assert world["case1"] == _case1(world, created_at=_ts(world["case1"]["created_at"]))


def test_分级审核逐格推进_机构锚点随审核者走(client, auth, world):
    ts = _ts(world["case1"]["created_at"])
    assert world["review1"] == _case1(
        world, status="township_reviewed", current_level="township",
        current_org_id=world["township"]["id"], created_at=ts,
    )
    assert world["review2"] == _case1(
        world, status="accepted", current_level="county",
        current_org_id=world["county"]["id"], created_at=ts,
    )
    assert world["arrived"] == _case1(
        world, status="arrived", current_level="county",
        current_org_id=world["county"]["id"], effective_visit=True, created_at=ts,
    )
    assert world["down"] == _case1(
        world, status="down_referred", current_level="township",
        current_org_id=world["township"]["id"], target_org_id=world["township"]["id"],
        effective_visit=True, stable_for_down=True, created_at=ts,
    )
    assert world["closed"] == _case1(
        world, status="closed", current_level="township",
        current_org_id=world["township"]["id"], target_org_id=world["township"]["id"],
        effective_visit=True, stable_for_down=True, created_at=ts,
        closed_at=_ts(world["closed"]["closed_at"]),  # 闭环才有 ISO 串
    )


def test_详情端点独有steps键且在末尾(client, auth, world):
    body = client.get(f"{B}/referrals/{world['case1']['id']}", headers=auth).json()
    assert list(body.keys()) == CASE_KEYS + ["steps"]
    assert [list(s.keys()) for s in body["steps"]] == [STEP_KEYS] * 6
    v, t, c = world["village"]["id"], world["township"]["id"], world["county"]["id"]
    expected = [
        ("发起", "submit", world["vdoc_id"], v, "血压持续不达标"),
        ("卫生院审核", "pass", world["tdoc_id"], t, "同意上转"),
        ("县级医院接收", "pass", world["cdoc_id"], c, "接收住院"),
        ("到院", "arrive", world["cdoc_id"], c, "已到院"),
        ("下转", "down", world["cdoc_id"], c, "情况稳定下转"),
        ("随访接收", "receive", world["tdoc_id"], t, "已接收随访"),
    ]
    assert body["steps"] == [
        {"id": s["id"], "step": step, "action": action, "actor_id": actor,
         "org_id": org, "opinion": opinion, "created_at": _ts(s["created_at"])}
        for s, (step, action, actor, org, opinion) in zip(body["steps"], expected)
    ]
    assert body == {
        **_case1(world, status="closed", current_level="township",
                 current_org_id=world["township"]["id"],
                 target_org_id=world["township"]["id"], effective_visit=True,
                 stable_for_down=True, created_at=_ts(body["created_at"]),
                 closed_at=_ts(body["closed_at"])),
        "steps": body["steps"],
    }


def test_撤回与退回回执(client, auth, world):
    body = world["withdrawn"]
    assert list(body.keys()) == CASE_KEYS
    assert body == {
        **_case1(world, created_at=_ts(body["created_at"])),
        "id": world["case2"]["id"], "reason": "误开撤回", "materials": [],
        "status": "withdrawn", "closed_at": _ts(body["closed_at"]),
    }
    rejected = world["rejected"]
    assert rejected == {
        **_case1(world, created_at=_ts(rejected["created_at"])),
        "id": world["case3"]["id"], "reason": "资料待补", "materials": [],
        "status": "rejected", "closed_at": _ts(rejected["closed_at"]),
    }


def test_列表行与发起回执同形无steps(client, auth, world):
    resp = client.get(f"{B}/referrals", headers=auth)
    rows = resp.json()
    assert resp.headers["x-total-count"] == "5"
    assert [list(r.keys()) for r in rows] == [CASE_KEYS] * 5  # 谁都不带 steps
    assert [r["id"] for r in rows] == [
        world["case4"]["id"], world["case3"]["id"], world["case2"]["id"],
        world["case1"]["id"], world["check_auto"]["case"]["id"],
    ]
    assert rows[3] == {
        **_case1(world, status="closed", current_level="township",
                 current_org_id=world["township"]["id"],
                 target_org_id=world["township"]["id"], effective_visit=True,
                 stable_for_down=True, created_at=_ts(rows[3]["created_at"]),
                 closed_at=_ts(rows[3]["closed_at"])),
    }
    only_open = client.get(f"{B}/referrals", params={"open_only": True},
                           headers=auth).json()
    assert [r["id"] for r in only_open] == [world["case4"]["id"],
                                            world["check_auto"]["case"]["id"]]


def test_闭环率统计完整精确(client, auth, world):
    body = client.get(f"{B}/referrals-stats/closure", headers=auth).json()
    assert list(body.keys()) == ["total", "denominator", "closed", "closure_rate",
                                 "effective_visits", "effective_rate", "by_status",
                                 "pending_by_level"]
    # 分母不含 withdrawn/rejected：5 - 1 - 1 = 3
    assert body == {
        "total": 5, "denominator": 3, "closed": 1, "closure_rate": 33.3,
        "effective_visits": 1, "effective_rate": 33.3,
        "by_status": {"accepted": 1, "closed": 1, "rejected": 1, "submitted": 1,
                      "withdrawn": 1},
        "pending_by_level": {"county": 1, "village": 1},
    }
    assert isinstance(body["closure_rate"], float)


def test_超时预警键序与回声参数(client, auth, world):
    body = client.get(f"{B}/referrals-alerts", params={"hours": 48}, headers=auth).json()
    assert list(body.keys()) == ["threshold_hours", "count", "items"]
    # 单据都是刚开的，48 小时口径下无在途超时——items 行形状与 CASE_KEYS 同源，
    # 已由发起/列表/详情钉住；这里钉住顶层三键与"无超时即空表"的当前字节
    assert body == {"threshold_hours": 48, "count": 0, "items": []}
    assert isinstance(body["threshold_hours"], int)


# ============================================================ 错误体与越权


def test_各类错误体都只有detail(client, auth, world):
    cid_auto = world["check_auto"]["case"]["id"]
    cases = [
        (client.post(f"{B}/referral-rules",
                     json={"code": "ct_e1", "name": "空条件", "conditions": []},
                     headers=auth), 422),
        (client.post(f"{B}/referral-rules",
                     json={"code": "ct_e2", "name": "坏比较符",
                           "conditions": [{"field": "bp_sys", "op": "≥", "value": 1}]},
                     headers=auth), 422),
        (client.post(f"{B}/referral-rules",
                     json={"code": "ct_ref_rule", "name": "重复",
                           "conditions": [COND]}, headers=auth), 409),
        (client.patch(f"{B}/referral-rules/999999", json={"name": "x"},
                      headers=auth), 404),
        (client.patch(f"{B}/referral-rules/{world['rule']['id']}",
                      json={"conditions": [{"field": "", "op": ">="}]},
                      headers=auth), 422),
        (client.post(f"{B}/referrals",
                     json={"patient_id": world["patient"]["id"],
                           "target_org_id": 999999}, headers=auth), 404),
        # admin 不绑机构且患者未纳管：无法确定发起机构
        (client.post(f"{B}/referrals",
                     json={"patient_id": world["patient"]["id"], "reason": "代录"},
                     headers=auth), 422),
        (client.get(f"{B}/referrals/999999", headers=auth), 404),
        (client.post(f"{B}/referrals/999999/review", json={"action": "pass"},
                     headers=auth), 404),
        # 终态单不接受审核
        (client.post(f"{B}/referrals/{world['case1']['id']}/review",
                     json={"action": "pass"}, headers=auth), 409),
        # 村医不是当前机构（村卫生室）的上级，不能审自己发的单
        (client.post(f"{B}/referrals/{cid_auto}/review", json={"action": "pass"},
                     headers=world["vdoc"]), 403),
        (client.post(f"{B}/referrals/999999/arrive", json={}, headers=auth), 404),
        (client.post(f"{B}/referrals/{cid_auto}/arrive", json={}, headers=auth), 409),
        (client.post(f"{B}/referrals/999999/down", json={"target_org_id": 1},
                     headers=auth), 404),
        (client.post(f"{B}/referrals/{cid_auto}/down",
                     json={"target_org_id": world["township"]["id"]},
                     headers=auth), 409),
        # 已接收的单下转到不存在的机构
        (client.post(f"{B}/referrals/{world['case4']['id']}/down",
                     json={"target_org_id": 999999}, headers=world["cdoc"]), 404),
        (client.post(f"{B}/referrals/999999/receive-followup", json={},
                     headers=auth), 404),
        (client.post(f"{B}/referrals/{cid_auto}/receive-followup", json={},
                     headers=auth), 409),
        (client.post(f"{B}/referrals/999999/withdraw", headers=auth), 404),
        # 非发起人（乡镇医生）不能撤别人的单
        (client.post(f"{B}/referrals/{cid_auto}/withdraw", headers=world["tdoc"]), 403),
        # 已进入上级审核（此处已闭环）的单不能撤回
        (client.post(f"{B}/referrals/{world['case1']['id']}/withdraw",
                     headers=auth), 409),
    ]
    for resp, expected in cases:
        assert resp.status_code == expected, f"{resp.request.url} -> {resp.text}"
        assert set(resp.json()) == {"detail"}
