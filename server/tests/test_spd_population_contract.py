"""慢专病人群域 `spd/population`（29 端点，28 补契约 + 1 既有 204）的**响应契约**特征化网。

场景全部经 HTTP API 种出：自建两级机构、四名患者、自建病种（含纳入规则与阶段）、
量表（发布）、服务包、路径模板（发布并启动实例）、居民端短信登录提交服务申请。
每个端点断言**完整精确 JSON**（dict 相等），代表性端点另钉**键序**——
Pydantic 序列化按声明顺序走，键序漂了就是字节变了。

四处易错的建模判断，在这里用数据钉死：

1. **患者简要信息是条件键**：新建/复核/认领这类单条回执**不带** patient_name 等
   brief 键（键整个不出现，不是 null），列表行才带——同一模型 + exclude_unset，
   两个方向各断言一次键序。
2. `screenings.score` 是 **Float 列**（量表评分也恒 float）：3 分读回来是 `3.0`；
   而服务包 `price` 是 **Money 列**：整数价读回来是 `int`（200 就是 200，不是
   200.0）。dict 相等比不出 `3 == 3.0`，所以另加 isinstance 断言。
3. **生命周期回执两种形状**：resume 只有 enrollment+closed 两个键（event_id/
   pending_confirm 整个不在）；跨机构迁出 closed 是空 `{}`，本地事件是四项计数
   ——`closed` 只能宽字典。
4. `claimed_at`/`read_at` 一类是"空串或 ISO 串"，**不是 null**。
"""
from datetime import date, datetime, timedelta

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
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/spd"
TODAY = date.today().isoformat()


def _iso(value: str) -> str:
    """校验是 ISO 日期时间串后原样返回（时间戳不可预置，取回来钉格式）。"""
    datetime.fromisoformat(value)
    return value


def _age_of(birth_date: str) -> int:
    born = date.fromisoformat(birth_date)
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


SCREENING_KEYS = ["id", "patient_id", "program_code", "source", "org_id", "scale_code",
                  "score", "risk_level", "result", "advice", "reviewed", "review_result",
                  "review_note", "answers", "created_at"]
CANDIDATE_KEYS = ["id", "patient_id", "program_code", "status", "source", "org_id",
                  "team_id", "assigned_user_id", "risk_level", "reason", "matched_rules",
                  "claimed_at", "created_at"]
ENROLL_KEYS = ["id", "patient_id", "program_code", "org_id", "team_id", "doctor_user_id",
               "manager_user_id", "village_doctor_id", "stage", "risk_level", "status",
               "source", "sign_date", "consent_signed", "consent_no", "service_start",
               "service_end", "archived", "habits", "risk_factors", "complications",
               "tags", "last_followup_at", "next_followup_at", "created_at"]
BRIEF_KEYS = ["patient_name", "gender", "birth_date", "phone"]
BINDING_KEYS = ["id", "enrollment_id", "package_id", "package_name", "price", "items",
                "status", "period_end", "bound_at", "usage_rate", "remaining"]

# 病种配置 API 会把规则归一化（补 label 等键）后落库，目标池 matched_rules 里
# 回来的是**落库后的**形状——期望值从建档响应取，不抄输入
AGE_RULE_IN = {"field": "age", "op": ">=", "value": 60}


@pytest.fixture(scope="module")
def base(client, h):
    """机构×2、患者×4、病种（纳入规则 age>=60 + 阶段 s1）、量表（发布）、服务包。"""
    org = client.post(
        "/api/organizations",
        json={"name": "契约人群卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "契约人群县医院", "org_type": "lead_hospital", "level": "county"},
        headers=h,
    ).json()
    p_screen = client.post(
        "/api/patients",
        json={"name": "契约人群一", "id_card": "330881195006155566", "gender": "男",
              "birth_date": "1950-06-15", "phone": "13900030001"},
        headers=h,
    ).json()
    p_life = client.post(
        "/api/patients",
        json={"name": "契约人群二", "id_card": "330881196203082233", "gender": "女",
              "birth_date": "1962-03-08", "phone": "13900030002"},
        headers=h,
    ).json()
    p_apply = client.post(
        "/api/patients",
        json={"name": "契约人群三", "id_card": "330881197007074444", "gender": "女",
              "birth_date": "1970-07-07", "phone": "13900030003"},
        headers=h,
    ).json()
    p_auto = client.post(
        "/api/patients",
        json={"name": "契约人群四", "id_card": "330881194001011122", "gender": "男",
              "birth_date": "1940-01-01"},
        headers=h,
    ).json()
    program = client.post(
        f"{B}/programs",
        json={"code": "ctp_dm", "name": "契约糖尿病", "category": "chronic",
              "include_rules": [AGE_RULE_IN], "stages": [{"key": "s1", "name": "一期"}]},
        headers=h,
    ).json()
    scale = client.post(
        f"{B}/scales",
        json={"code": "ctp_scale", "name": "契约人群量表", "category": "risk",
              "program_code": "ctp_dm",
              "items": [
                  {"key": "q1", "title": "多饮", "type": "single",
                   "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
                  {"key": "q2", "title": "多尿", "type": "single",
                   "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
              ],
              "scoring": {"ranges": [
                  {"min": 0, "max": 1, "risk": "low", "advice": "保持现状"},
                  {"min": 2, "max": 99, "risk": "high", "advice": "尽快复核建档"},
              ]}},
        headers=h,
    ).json()
    client.post(f"{B}/scales/{scale['id']}/publish", headers=h)
    package = client.post(
        f"{B}/service-packages",
        json={"code": "ctp_pkg", "name": "契约服务包", "program_code": "ctp_dm",
              "price": 200, "period_days": 30,
              "items": [{"code": "bp_check", "name": "血压测量", "times": 4, "price": 5},
                        {"code": "edu", "name": "健康宣教", "times": 1, "price": 12.5}]},
        headers=h,
    ).json()
    return {"org": org, "org2": org2, "p_screen": p_screen, "p_life": p_life,
            "p_apply": p_apply, "p_auto": p_auto, "program": program, "scale": scale,
            "package": package, "age_rule": program["include_rules"][0]}


# ------------------------------------------------- 筛查


def test_筛查登记复核与列表(client, h, base):
    pid, org_id = base["p_screen"]["id"], base["org"]["id"]
    resp = client.post(
        f"{B}/screenings",
        json={"patient_id": pid, "program_code": "ctp_dm", "source": "active",
              "org_id": org_id, "scale_code": "ctp_scale",
              "answers": {"q1": "是", "q2": "是"}},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 单条回执**不带** patient_name/gender（条件键整个不在，不是 null）
    assert list(body) == SCREENING_KEYS
    assert body == {
        "id": body["id"], "patient_id": pid, "program_code": "ctp_dm",
        "source": "active", "org_id": org_id, "scale_code": "ctp_scale", "score": 3.0,
        "risk_level": "high", "result": "suspect", "advice": "尽快复核建档",
        "reviewed": False, "review_result": "", "review_note": "",
        "answers": {"q1": "是", "q2": "是"}, "created_at": _iso(body["created_at"]),
    }
    # Float 列：3 分读回来是 3.0（写成 int 契约就把字节改了）
    assert isinstance(body["score"], float)
    base["screening"] = body

    rows = client.get(f"{B}/screenings", params={"patient_id": pid}, headers=h).json()
    assert [list(r) for r in rows] == [SCREENING_KEYS + ["patient_name", "gender"]]
    assert rows == [{**body, "patient_name": "契约人群一", "gender": "男"}]

    reviewed = client.post(
        f"{B}/screenings/{body['id']}/review",
        json={"review_result": "confirmed", "review_note": "复核确认"},
        headers=h,
    )
    assert reviewed.status_code == 200
    assert reviewed.json() == {**body, "reviewed": True, "review_result": "confirmed",
                               "review_note": "复核确认"}

    cands = client.get(f"{B}/candidates",
                       params={"program_code": "ctp_dm", "status": "target"},
                       headers=h).json()
    assert [list(c) for c in cands] == [CANDIDATE_KEYS + BRIEF_KEYS]
    assert cands == [{
        "id": cands[0]["id"], "patient_id": pid, "program_code": "ctp_dm",
        "status": "target", "source": "screening", "org_id": org_id, "team_id": None,
        "assigned_user_id": None, "risk_level": "high", "reason": "age",
        "matched_rules": [base["age_rule"]], "claimed_at": "",
        "created_at": _iso(cands[0]["created_at"]),
        "patient_name": "契约人群一", "gender": "男", "birth_date": "1950-06-15",
        "phone": "13900030001",
    }]
    base["cand_screen"] = cands[0]


def test_自动识别批量筛查(client, h, base):
    org2 = base["org2"]["id"]
    client.post("/api/encounters",
                json={"patient_id": base["p_auto"]["id"], "org_id": org2}, headers=h)
    resp = client.post(f"{B}/screenings/auto-run",
                       json={"program_code": "ctp_dm", "org_id": org2}, headers=h)
    assert resp.status_code == 200, resp.text
    assert list(resp.json()) == ["scanned", "suspect", "excluded", "normal", "rule_version"]
    # rule_version 是 String 列（"v1"），不是数字
    assert resp.json() == {"scanned": 1, "suspect": 1, "excluded": 0, "normal": 0,
                           "rule_version": "v1"}


def test_目标池分发认领改状态(client, h, base):
    org2 = base["org2"]["id"]
    cand = client.get(f"{B}/candidates",
                      params={"program_code": "ctp_dm", "org_id": org2},
                      headers=h).json()[0]
    assert cand["source"] == "auto" and cand["status"] == "suspect"

    dist = client.post(f"{B}/candidates/distribute",
                       json={"candidate_ids": [cand["id"], 999999]}, headers=h)
    assert dist.json() == {"distributed": 1, "not_found": 1}

    claimed = client.post(f"{B}/candidates/{cand['id']}/claim", headers=h)
    assert claimed.status_code == 200
    body = claimed.json()
    # 认领回执不带 brief：13 个键（条件键方向二）
    assert list(body) == CANDIDATE_KEYS
    admin_id = body["assigned_user_id"]
    assert isinstance(admin_id, int)
    assert body == {
        "id": cand["id"], "patient_id": base["p_auto"]["id"], "program_code": "ctp_dm",
        "status": "target", "source": "auto", "org_id": org2, "team_id": None,
        "assigned_user_id": admin_id, "risk_level": "mid", "reason": "age",
        "matched_rules": [base["age_rule"]], "claimed_at": _iso(body["claimed_at"]),
        "created_at": cand["created_at"],
    }
    base["admin_id"] = admin_id

    excluded = client.post(f"{B}/candidates/{cand['id']}/status",
                           json={"status": "excluded", "reason": "暂不符合"}, headers=h)
    assert excluded.json() == {**body, "status": "excluded", "reason": "暂不符合"}


# ------------------------------------------------- 签约建档纳管


def test_签约建档与在管列表(client, h, base):
    pid, org_id = base["p_screen"]["id"], base["org"]["id"]
    created = client.post(
        f"{B}/enrollments",
        json={"patient_id": pid, "program_code": "ctp_dm", "org_id": org_id,
              "sign_date": TODAY, "consent_signed": True, "consent_no": "TZ-001",
              "service_start": TODAY, "tags": ["重点人群"]},
        headers=h,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert list(body) == ENROLL_KEYS
    expected = {
        "id": body["id"], "patient_id": pid, "program_code": "ctp_dm", "org_id": org_id,
        "team_id": None, "doctor_user_id": None, "manager_user_id": None,
        "village_doctor_id": None, "stage": "s1", "risk_level": "low",
        "status": "active", "source": "screening", "sign_date": TODAY,
        "consent_signed": True, "consent_no": "TZ-001", "service_start": TODAY,
        "service_end": "", "archived": False, "habits": {}, "risk_factors": [],
        "complications": [], "tags": ["重点人群"], "last_followup_at": "",
        "next_followup_at": "", "created_at": _iso(body["created_at"]),
    }
    assert body == expected
    base["enroll"] = body

    next_at = (date.today() + timedelta(days=30)).isoformat()
    patched = client.patch(
        f"{B}/enrollments/{body['id']}",
        json={"habits": {"smoke": "偶尔"}, "risk_factors": ["吸烟"],
              "next_followup_at": next_at},
        headers=h,
    )
    assert patched.status_code == 200
    # 三项关键信息任一有值即视为已建档（archived 联动）
    expected2 = {**expected, "habits": {"smoke": "偶尔"}, "risk_factors": ["吸烟"],
                 "next_followup_at": next_at, "archived": True}
    assert patched.json() == expected2
    base["enroll"] = expected2

    rows = client.get(f"{B}/enrollments",
                      params={"program_code": "ctp_dm", "org_id": org_id},
                      headers=h).json()
    assert [list(r) for r in rows] == [ENROLL_KEYS + BRIEF_KEYS + ["ehc_no"]]
    full_row = {**expected2, "patient_name": "契约人群一", "gender": "男",
                "birth_date": "1950-06-15", "phone": "13900030001",
                "ehc_no": base["p_screen"]["ehc_no"]}
    assert rows == [full_row]
    assert client.get(f"{B}/enrollments", params={"keyword": "契约人群一"},
                      headers=h).json() == [full_row]
    base["enroll_row"] = full_row


# ------------------------------------------------- 服务包绑定与扣减


def test_服务包绑定与扣减(client, h, base):
    eid, pkg = base["enroll"]["id"], base["package"]
    period_end = (date.today() + timedelta(days=30)).isoformat()
    bound = client.post(f"{B}/enrollments/{eid}/packages",
                        json={"package_id": pkg["id"]}, headers=h)
    assert bound.status_code == 201, bound.text
    body = bound.json()
    assert list(body) == BINDING_KEYS
    assert body == {
        "id": body["id"], "enrollment_id": eid, "package_id": pkg["id"],
        "package_name": "契约服务包", "price": 200,
        "items": [{"code": "bp_check", "name": "血压测量", "total": 4, "used": 0, "price": 5},
                  {"code": "edu", "name": "健康宣教", "total": 1, "used": 0, "price": 12.5}],
        "status": "bound", "period_end": period_end, "bound_at": _iso(body["bound_at"]),
        "usage_rate": 0.0, "remaining": 5,
    }
    # Money 列：整数价原样是 int（声明 float 会把 200 变 200.0）；比率恒 float
    assert isinstance(body["price"], int)
    assert isinstance(body["items"][0]["price"], int)
    assert isinstance(body["items"][1]["price"], float)
    assert isinstance(body["usage_rate"], float)
    base["binding"] = body

    again = client.post(f"{B}/enrollments/{eid}/packages",
                        json={"package_id": pkg["id"]}, headers=h)
    assert again.status_code == 409

    u1 = client.post(f"{B}/package-bindings/{body['id']}/usages",
                     json={"item_code": "bp_check", "note": "首次测量"}, headers=h)
    assert u1.status_code == 201
    assert list(u1.json()) == ["usage_id", "binding"]
    after1 = {**body,
              "items": [{"code": "bp_check", "name": "血压测量", "total": 4, "used": 1,
                         "price": 5},
                        {"code": "edu", "name": "健康宣教", "total": 1, "used": 0,
                         "price": 12.5}],
              "usage_rate": 20.0, "remaining": 4}
    assert u1.json() == {"usage_id": u1.json()["usage_id"], "binding": after1}

    u2 = client.post(f"{B}/package-bindings/{body['id']}/usages",
                     json={"item_code": "edu"}, headers=h)
    after2 = {**body,
              "items": [{"code": "bp_check", "name": "血压测量", "total": 4, "used": 1,
                         "price": 5},
                        {"code": "edu", "name": "健康宣教", "total": 1, "used": 1,
                         "price": 12.5}],
              "usage_rate": 40.0, "remaining": 3}
    assert u2.json()["binding"] == after2
    base["binding_used"] = after2

    # 剩余次数不足与项目不存在，各自给明确原因
    assert client.post(f"{B}/package-bindings/{body['id']}/usages",
                       json={"item_code": "edu"}, headers=h).status_code == 409
    assert client.post(f"{B}/package-bindings/{body['id']}/usages",
                       json={"item_code": "nope"}, headers=h).status_code == 404

    rows = client.get(f"{B}/package-bindings/{body['id']}/usages", headers=h).json()
    assert [list(r) for r in rows] == [["id", "item_code", "item_name", "qty", "price",
                                        "note", "used_at"]] * 2
    assert rows == [
        {"id": rows[0]["id"], "item_code": "edu", "item_name": "健康宣教", "qty": 1,
         "price": 12.5, "note": "", "used_at": _iso(rows[0]["used_at"])},
        {"id": rows[1]["id"], "item_code": "bp_check", "item_name": "血压测量", "qty": 1,
         "price": 5, "note": "首次测量", "used_at": _iso(rows[1]["used_at"])},
    ]
    assert isinstance(rows[0]["price"], float) and isinstance(rows[1]["price"], int)


def test_路径启动与档案详情(client, h, base):
    eid = base["enroll"]["id"]
    template = client.post(
        f"{B}/path-templates",
        json={"program_id": base["program"]["id"], "code": "ctp_path",
              "name": "契约慢病路径", "scene": "followup"},
        headers=h,
    ).json()
    client.post(f"{B}/path-templates/{template['id']}/nodes",
                json={"key": "n1", "name": "随访评估", "stage": "s1", "seq": 1},
                headers=h)
    client.post(f"{B}/path-templates/{template['id']}/status",
                json={"status": "published"}, headers=h)
    instance = client.post(f"{B}/path-instances",
                           json={"enrollment_id": eid, "template_id": template["id"]},
                           headers=h).json()
    base["instance"] = instance

    detail = client.get(f"{B}/enrollments/{eid}", headers=h).json()
    # 详情 = 列表行的严格超集：brief 之后再接 packages/paths
    assert list(detail) == ENROLL_KEYS + BRIEF_KEYS + ["ehc_no", "packages", "paths"]
    assert detail == {
        **base["enroll_row"],
        "packages": [base["binding_used"]],
        "paths": [{"id": instance["id"], "template_code": "ctp_path",
                   "status": "running", "current_node_key": "n1",
                   "current_stage": "s1", "progress": 0}],
    }


def test_服务包解绑(client, h, base):
    unbound = client.post(f"{B}/package-bindings/{base['binding']['id']}/unbind",
                          headers=h)
    assert unbound.status_code == 200
    assert unbound.json() == {**base["binding_used"], "status": "unbound"}
    base["binding_used"] = unbound.json()


# ------------------------------------------------- 生命周期


def test_生命周期召回迁出确认与事件列表(client, h, base):
    org, org2 = base["org"]["id"], base["org2"]["id"]
    p_life = base["p_life"]["id"]
    e2 = client.post(
        f"{B}/enrollments",
        json={"patient_id": p_life, "program_code": "ctp_dm", "org_id": org},
        headers=h,
    ).json()

    zero_closed = {"tasks": 0, "instances": 0, "interventions": 0, "revisits": 0}
    recalled = client.post(f"{B}/enrollments/{e2['id']}/lifecycle",
                           json={"event": "recall", "reason": "失访三月"}, headers=h)
    assert recalled.status_code == 200, recalled.text
    body = recalled.json()
    assert list(body) == ["enrollment", "event_id", "pending_confirm", "closed"]
    assert list(body["enrollment"]) == ENROLL_KEYS
    assert body == {"enrollment": {**e2, "status": "recalled"},
                    "event_id": body["event_id"], "pending_confirm": False,
                    "closed": zero_closed}

    recalls = client.get(f"{B}/recalls", headers=h).json()
    assert [list(r) for r in recalls] == [["id", "enrollment_id", "reason", "status",
                                           "result", "contacts", "created_at"]]
    assert recalls == [{"id": recalls[0]["id"], "enrollment_id": e2["id"],
                        "reason": "失访三月", "status": "pending", "result": "",
                        "contacts": [], "created_at": _iso(recalls[0]["created_at"])}]
    rid = recalls[0]["id"]

    contacted = client.post(f"{B}/recalls/{rid}/progress",
                            json={"status": "contacted", "contact_note": "电话已接"},
                            headers=h)
    assert contacted.json() == {"id": rid, "status": "contacted", "result": ""}
    returned = client.post(f"{B}/recalls/{rid}/progress",
                           json={"status": "returned", "result": "已回访"}, headers=h)
    assert returned.json() == {"id": rid, "status": "returned", "result": "已回访"}
    assert client.get(f"{B}/recalls", params={"status": "returned"}, headers=h).json() == [{
        "id": rid, "enrollment_id": e2["id"], "reason": "失访三月", "status": "returned",
        "result": "已回访", "contacts": [{"at": TODAY, "note": "电话已接"}],
        "created_at": recalls[0]["created_at"],
    }]

    # 跨机构迁出：待确认，原档案保持在管，closed 是空 {}
    migrated = client.post(f"{B}/enrollments/{e2['id']}/lifecycle",
                           json={"event": "migrate", "reason": "搬迁",
                                 "target_org_id": org2}, headers=h)
    mbody = migrated.json()
    assert mbody == {"enrollment": {**e2, "status": "active"},
                     "event_id": mbody["event_id"], "pending_confirm": True,
                     "closed": {}}

    confirmed = client.post(f"{B}/lifecycle-events/{mbody['event_id']}/confirm",
                            headers=h)
    assert confirmed.status_code == 200, confirmed.text
    cbody = confirmed.json()
    assert list(cbody) == ["enrollment", "incoming_enrollment", "closed"]
    e3 = cbody["incoming_enrollment"]
    assert cbody == {
        "enrollment": {**e2, "status": "migrated"},
        "incoming_enrollment": {
            "id": e3["id"], "patient_id": p_life, "program_code": "ctp_dm",
            "org_id": org2, "team_id": None, "doctor_user_id": None,
            "manager_user_id": None, "village_doctor_id": None, "stage": "s1",
            "risk_level": "low", "status": "active", "source": "migrate",
            "sign_date": TODAY, "consent_signed": False, "consent_no": "",
            "service_start": "", "service_end": "", "archived": False, "habits": {},
            "risk_factors": [], "complications": [], "tags": [],
            "last_followup_at": "", "next_followup_at": "",
            "created_at": _iso(e3["created_at"]),
        },
        "closed": zero_closed,
    }

    excluded = client.post(f"{B}/enrollments/{e3['id']}/lifecycle",
                           json={"event": "exclude", "reason": "误纳"}, headers=h)
    assert excluded.json() == {"enrollment": {**e3, "status": "excluded"},
                               "event_id": excluded.json()["event_id"],
                               "pending_confirm": False, "closed": zero_closed}
    # resume 分支只有两个键：event_id / pending_confirm 整个不出现（条件键）
    resumed = client.post(f"{B}/enrollments/{e3['id']}/lifecycle",
                          json={"event": "resume", "reason": "重新纳管"}, headers=h)
    assert list(resumed.json()) == ["enrollment", "closed"]
    assert resumed.json() == {"enrollment": {**e3, "status": "active"}, "closed": {}}

    events = client.get(f"{B}/lifecycle-events", headers=h).json()
    keys = ["id", "enrollment_id", "event", "reason", "detail", "target_org_id",
            "confirmed", "occurred_at", "program_code", "patient_id", "patient_name",
            "created_at"]
    assert [list(r) for r in events] == [keys] * 4

    def _event(row, enrollment_id, event, reason, target):
        return {"id": row["id"], "enrollment_id": enrollment_id, "event": event,
                "reason": reason, "detail": "", "target_org_id": target,
                "confirmed": True, "occurred_at": TODAY, "program_code": "ctp_dm",
                "patient_id": p_life, "patient_name": "契约人群二",
                "created_at": _iso(row["created_at"])}

    assert events == [
        _event(events[0], e3["id"], "resume", "重新纳管", None),
        _event(events[1], e3["id"], "exclude", "误纳", None),
        _event(events[2], e2["id"], "migrate", "搬迁", org2),
        _event(events[3], e2["id"], "recall", "失访三月", None),
    ]
    assert client.get(f"{B}/lifecycle-events", params={"event": "migrate"},
                      headers=h).json() == [events[2]]
    base["e2"] = {**e2, "status": "migrated"}


# ------------------------------------------------- 患者分组


def test_患者分组(client, h, base):
    created = client.post(f"{B}/groups", json={"name": "契约重点组"}, headers=h)
    assert created.status_code == 201
    g1 = created.json()
    assert g1 == {"id": g1["id"], "name": "契约重点组", "scope": "personal",
                  "member_count": 0}

    rule = [{"field": "risk_level", "op": "==", "value": "low"}]
    g2 = client.post(f"{B}/groups",
                     json={"name": "契约自动组", "scope": "dept", "dept": "公卫科",
                           "auto_rule": rule},
                     headers=h).json()

    added = client.post(
        f"{B}/groups/{g1['id']}/members",
        json={"patient_ids": [base["p_screen"]["id"], base["p_apply"]["id"]]},
        headers=h,
    )
    assert added.json() == {"added": 2, "total": 2}
    # 自动规则筛在管人群：此刻在管且 risk_level=low 的是 p_screen 与迁入的 p_life
    auto_added = client.post(f"{B}/groups/{g2['id']}/members",
                             json={"use_auto_rule": True, "program_code": "ctp_dm"},
                             headers=h)
    assert auto_added.json() == {"added": 2, "total": 2}

    rows = client.get(f"{B}/groups", headers=h).json()
    assert [list(r) for r in rows] == [["id", "name", "scope", "dept", "owner_user_id",
                                        "auto_rule", "member_count", "updated_at"]] * 2
    assert rows == [
        {"id": g2["id"], "name": "契约自动组", "scope": "dept", "dept": "公卫科",
         "owner_user_id": base["admin_id"], "auto_rule": rule, "member_count": 2,
         "updated_at": _iso(rows[0]["updated_at"])},
        {"id": g1["id"], "name": "契约重点组", "scope": "personal", "dept": "",
         "owner_user_id": base["admin_id"], "auto_rule": [], "member_count": 2,
         "updated_at": _iso(rows[1]["updated_at"])},
    ]

    members = client.get(f"{B}/groups/{g1['id']}/members", headers=h).json()
    assert [list(m) for m in members] == [["id", "patient_id", "added_at", "name",
                                           "gender", "birth_date", "ehc_no", "phone"]] * 2
    assert members == [
        {"id": members[0]["id"], "patient_id": base["p_apply"]["id"],
         "added_at": _iso(members[0]["added_at"]), "name": "契约人群三", "gender": "女",
         "birth_date": "1970-07-07", "ehc_no": base["p_apply"]["ehc_no"],
         "phone": "13900030003"},
        {"id": members[1]["id"], "patient_id": base["p_screen"]["id"],
         "added_at": _iso(members[1]["added_at"]), "name": "契约人群一", "gender": "男",
         "birth_date": "1950-06-15", "ehc_no": base["p_screen"]["ehc_no"],
         "phone": "13900030001"},
    ]
    removed = client.delete(
        f"{B}/groups/{g1['id']}/members/{base['p_apply']['id']}", headers=h
    )
    assert removed.status_code == 204 and removed.content == b""
    base["group"] = g1


# ------------------------------------------------- 居民服务申请


def test_居民服务申请受理(client, h, base):
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": "13900030003"}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login",
                        json={"phone": "13900030003", "code": code})
    assert login.status_code == 200, login.text
    ph = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post("/api/portal/auth/realname",
                json={"name": "契约人群三", "id_card": "330881197007074444"}, headers=ph)
    applied = client.post("/api/portal/spd/service-applies",
                          json={"program_code": "ctp_dm", "note": "希望尽快纳管"},
                          headers=ph)
    assert applied.status_code == 201, applied.text
    aid = applied.json()["id"]

    rows = client.get(f"{B}/service-applies", headers=h).json()
    assert [list(r) for r in rows] == [["id", "patient_id", "program_code", "note",
                                        "status", "handle_note", "created_at", "name",
                                        "gender", "birth_date", "ehc_no", "phone"]]
    assert rows == [{
        "id": aid, "patient_id": base["p_apply"]["id"], "program_code": "ctp_dm",
        "note": "希望尽快纳管", "status": "pending", "handle_note": "",
        "created_at": _iso(rows[0]["created_at"]), "name": "契约人群三", "gender": "女",
        "birth_date": "1970-07-07", "ehc_no": base["p_apply"]["ehc_no"],
        "phone": "13900030003",
    }]

    handled = client.post(f"{B}/service-applies/{aid}/handle",
                          json={"status": "accepted", "handle_note": "已联系"}, headers=h)
    assert handled.json() == {"id": aid, "status": "accepted"}
    assert client.get(f"{B}/service-applies", headers=h).json() == []
    # 受理即入目标池
    cands = client.get(f"{B}/candidates",
                       params={"program_code": "ctp_dm", "status": "target"},
                       headers=h).json()
    assert cands == [{
        "id": cands[0]["id"], "patient_id": base["p_apply"]["id"],
        "program_code": "ctp_dm", "status": "target", "source": "apply", "org_id": None,
        "team_id": None, "assigned_user_id": base["admin_id"], "risk_level": "",
        "reason": "居民申请受理", "matched_rules": [], "claimed_at": "",
        "created_at": _iso(cands[0]["created_at"]), "patient_name": "契约人群三",
        "gender": "女", "birth_date": "1970-07-07", "phone": "13900030003",
    }]
    base["apply_id"] = aid


# ------------------------------------------------- 专病 360 档案


def test_专病360档案(client, h, base):
    pid = base["p_screen"]["id"]
    measured_at = f"{(date.today() - timedelta(days=1)).isoformat()}T08:00:00"
    client.post(f"{B}/measurements",
                json={"patient_id": pid, "metric": "bp_sys", "value": 160,
                      "unit": "mmHg", "measured_at": measured_at},
                headers=h)
    assessed = client.post(f"{B}/assessments",
                           json={"patient_id": pid, "scale_code": "ctp_scale",
                                 "answers": {}},
                           headers=h).json()
    referred = client.post(f"{B}/referrals",
                           json={"patient_id": pid, "program_code": "ctp_dm",
                                 "direction": "up", "reason": "血压异常"},
                           headers=h).json()

    body = client.get(f"{B}/patients/{pid}/profile", headers=h).json()
    assert list(body) == ["patient", "programs", "measurements", "assessments",
                          "referrals", "facts"]
    assert [list(p) for p in body["programs"]] == [["enrollment", "program_name",
                                                    "paths", "packages", "open_tasks",
                                                    "recent_tasks"]]
    program = body["programs"][0]
    # 卡片里的 enrollment 不带 brief（25 键），与列表行（30 键）是同一模型的两个方向
    assert list(program["enrollment"]) == ENROLL_KEYS
    task = program["recent_tasks"][0]
    assert body == {
        "patient": {"id": pid, "name": "契约人群一", "gender": "男",
                    "birth_date": "1950-06-15", "ehc_no": base["p_screen"]["ehc_no"],
                    "phone": "13900030001"},
        "programs": [{
            "enrollment": base["enroll"],
            "program_name": "契约糖尿病",
            "paths": [{"id": base["instance"]["id"], "template_code": "ctp_path",
                       "status": "running", "current_node_key": "n1", "progress": 0}],
            "packages": [base["binding_used"]],
            "open_tasks": 1,
            "recent_tasks": [{"id": task["id"], "title": "契约慢病路径·随访评估",
                              "task_type": "path", "status": "pending",
                              "due_date": (date.today() + timedelta(days=7)).isoformat()}],
        }],
        "measurements": [{"metric": "bp_sys", "value": 160.0, "unit": "mmHg",
                          "level": "normal", "source": "manual",
                          "measured_at": measured_at}],
        "assessments": [{"id": assessed["id"], "scale_code": "ctp_scale", "score": 0.0,
                         "risk_level": "low", "created_at": assessed["created_at"]}],
        "referrals": [{"id": referred["id"], "direction": "up", "status": "submitted",
                       "created_at": _iso(body["referrals"][0]["created_at"])}],
        "facts": {"age": _age_of("1950-06-15"), "gender": "男", "diagnosis": [],
                  "diagnosis_name": [], "bp_sys": 160.0},
    }
    # Float 列三处：监测值、评估分、事实字典里的最近值
    assert isinstance(body["measurements"][0]["value"], float)
    assert isinstance(body["assessments"][0]["score"], float)
    assert isinstance(body["facts"]["bp_sys"], float)


# ------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, h, base):
    eid = base["enroll"]["id"]
    cases = [
        client.post(f"{B}/screenings", headers=h,
                    json={"patient_id": base["p_screen"]["id"], "program_code": "nope"}),
        client.post(f"{B}/screenings/999999/review", headers=h,
                    json={"review_result": "confirmed"}),
        client.post(f"{B}/screenings/auto-run", headers=h,
                    json={"program_code": "nope"}),
        client.post(f"{B}/candidates/999999/claim", headers=h),
        client.post(f"{B}/candidates/{base['cand_screen']['id']}/status", headers=h,
                    json={"status": "target"}),   # 已纳管 → 409
        client.post(f"{B}/enrollments", headers=h,
                    json={"patient_id": base["p_screen"]["id"],
                          "program_code": "ctp_dm", "org_id": base["org"]["id"]}),
        client.get(f"{B}/enrollments/999999", headers=h),
        client.patch(f"{B}/enrollments/{base['e2']['id']}", headers=h,
                     json={"stage": "s1"}),        # 非在管 → 409
        client.post(f"{B}/enrollments/{eid}/lifecycle", headers=h,
                    json={"event": "migrate", "target_org_id": 999999}),
        client.post(f"{B}/lifecycle-events/999999/confirm", headers=h),
        client.post(f"{B}/recalls/999999/progress", headers=h,
                    json={"status": "contacted"}),
        client.post(f"{B}/groups/999999/members", headers=h, json={"patient_ids": [1]}),
        client.post(f"{B}/groups/{base['group']['id']}/members", headers=h,
                    json={"use_auto_rule": True}),  # 未配自动规则 → 422
        client.delete(f"{B}/groups/{base['group']['id']}/members/999999", headers=h),
        client.post(f"{B}/enrollments/999999/packages", headers=h,
                    json={"package_id": base["package"]["id"]}),
        client.post(f"{B}/enrollments/{eid}/packages", headers=h,
                    json={"package_id": 999999}),
        client.post(f"{B}/package-bindings/999999/usages", headers=h,
                    json={"item_code": "edu"}),
        client.post(f"{B}/package-bindings/{base['binding']['id']}/usages", headers=h,
                    json={"item_code": "edu"}),    # 已解绑 → 409
        client.post(f"{B}/package-bindings/999999/unbind", headers=h),
        client.post(f"{B}/service-applies/999999/handle", headers=h,
                    json={"status": "accepted"}),
        client.post(f"{B}/service-applies/{base['apply_id']}/handle", headers=h,
                    json={"status": "rejected"}),  # 已处理 → 409
        client.get(f"{B}/patients/999999/profile", headers=h),
    ]
    assert [r.status_code for r in cases] == [404, 404, 404, 404, 409, 409, 404, 409,
                                              404, 404, 404, 404, 422, 404, 404, 404,
                                              404, 409, 404, 404, 409, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
