"""慢专病工作台域 `spd/workbench`（8 端点）的**响应契约**——加 `response_model` 前后的特征化网。

八个端点全是**只读聚合**，字节风险集中在三处，本文件用数据逐一钉死：

1. **分组字典的键随数据而变**（by_risk/by_program/by_type/by_status/by_channel），
   而 `by_org`/`team_patients` 的键是**机构/团队 id（int）**——JSON 里必然序列化成
   字符串键，契约要用 `dict[int, int]` 而不是 `dict[str, int]`。
2. **`workbench/team` 的角色条件键**：case_manager 多 packages+consults、expert 多
   team_patients+paths、member 多 interventions——键**整个不出现**，不是 null。
   三种角色各断言一次完整键序。
3. **比率恒为 float**（completion_rate/closure_rate/usage_rate/avg_success_rate/
   screening_conversion_rate）：两条分支（有数据做除法 / 无数据兜底 `0.0`）都返回
   float，声明 float 是原样。

场景经 HTTP API 种出：三级机构、两个自建病种、纳管三人（含团队/主管医生/村医
责任关系）、任务三态、筛查两条（一审一未审）、转诊、随访计划、监测、评估、
数据源、专病中心、发布一条路径、跑一次考核出分。种子目录（病种/量表）是启动
种子 + 本场景自建的并集，涉及它们的期望值从配置 API 现算，不抄死数字。
"""
from datetime import date, datetime

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


def _login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/spd"
YES = {"family": "是", "salt": "是", "overweight": "是", "smoke": "是",
       "drink": "是", "symptom": "是"}
NO = {key: "否" for key in YES}


@pytest.fixture(scope="module")
def wb(client, h):
    """整套场景经 HTTP API 建数据（详见模块 docstring）。"""
    county = client.post(
        "/api/organizations",
        json={"name": "契约县医院", "org_type": "lead_hospital", "level": "county"},
        headers=h,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "契约卫生院", "org_type": "township", "level": "township",
              "parent_id": county["id"]},
        headers=h,
    ).json()
    village = client.post(
        "/api/organizations",
        json={"name": "契约卫生室", "org_type": "village", "level": "village",
              "parent_id": township["id"]},
        headers=h,
    ).json()
    doc = client.post(
        "/api/users",
        json={"username": "wb_doc", "password": "pass123456", "role": "doctor",
              "full_name": "契约医生", "org_id": township["id"]},
        headers=h,
    ).json()
    vd = client.post(
        "/api/users",
        json={"username": "wb_vd", "password": "pass123456", "role": "doctor",
              "full_name": "契约村医", "org_id": village["id"]},
        headers=h,
    ).json()
    wp1 = client.post(
        "/api/patients",
        json={"name": "契约甲", "id_card": "330882198001011111", "gender": "男",
              "birth_date": "1980-01-01", "phone": "13900020001"},
        headers=h,
    ).json()
    wp2 = client.post(
        "/api/patients",
        json={"name": "契约乙", "id_card": "330882195002022222", "gender": "女",
              "birth_date": "1950-02-02"},
        headers=h,
    ).json()
    wp3 = client.post(
        "/api/patients",
        json={"name": "契约丙", "id_card": "330882199503033333"},
        headers=h,
    ).json()
    prog_a = client.post(
        f"{B}/programs",
        json={"code": "wbp_a", "name": "契约甲病", "category": "chronic",
              "include_rules": [{"field": "age", "op": ">=", "value": 60}],
              "stages": [{"key": "s1", "name": "一期"}]},
        headers=h,
    ).json()
    client.post(
        f"{B}/programs",
        json={"code": "wbp_b", "name": "契约乙病", "category": "specialty",
              "include_rules": [{"field": "age", "op": ">=", "value": 18}]},
        headers=h,
    )
    team = client.post(
        f"{B}/teams",
        json={"name": "契约团队", "org_id": township["id"], "level": "township",
              "program_codes": ["wbp_a"], "leader_user_id": doc["id"]},
        headers=h,
    ).json()
    client.post(f"{B}/teams/{team['id']}/members",
                json={"user_id": doc["id"], "member_role": "doctor"}, headers=h)
    client.post(f"{B}/teams/{team['id']}/members",
                json={"user_id": vd["id"], "member_role": "village_doctor"}, headers=h)
    client.post(
        f"{B}/village-doctors",
        json={"user_id": vd["id"], "org_id": village["id"], "township": "契镇",
              "village": "契村"},
        headers=h,
    )
    e1 = client.post(
        f"{B}/enrollments",
        json={"patient_id": wp1["id"], "program_code": "wbp_a", "org_id": township["id"],
              "team_id": team["id"], "doctor_user_id": doc["id"],
              "manager_user_id": doc["id"], "village_doctor_id": vd["id"],
              "risk_level": "mid"},
        headers=h,
    ).json()
    client.post(
        f"{B}/enrollments",
        json={"patient_id": wp2["id"], "program_code": "wbp_b", "org_id": township["id"],
              "risk_level": "high"},
        headers=h,
    )
    client.post(
        f"{B}/enrollments",
        json={"patient_id": wp3["id"], "program_code": "wbp_a", "org_id": village["id"],
              "risk_level": "low"},
        headers=h,
    )
    # 任务三态：tA 今日到期（责任人=主管医生）、tB 直接办结、tC 待分配
    client.post(
        f"{B}/tasks",
        json={"patient_id": wp1["id"], "title": "契约随访A", "task_type": "followup",
              "program_code": "wbp_a", "enrollment_id": e1["id"], "due_days": 0},
        headers=h,
    )
    t_b = client.post(
        f"{B}/tasks",
        json={"patient_id": wp2["id"], "title": "契约干预B", "task_type": "intervention",
              "program_code": "wbp_b", "org_id": township["id"], "due_days": 5},
        headers=h,
    ).json()
    client.post(f"{B}/tasks/{t_b['id']}/complete", json={}, headers=h)
    client.post(
        f"{B}/tasks",
        json={"patient_id": wp3["id"], "title": "契约评估C", "task_type": "assess",
              "program_code": "wbp_a", "org_id": township["id"], "due_days": 5},
        headers=h,
    )
    # 筛查两条：s1 疑似未复核（乡），s2 疑似已复核确认（村，入目标池）
    client.post(
        f"{B}/screenings",
        json={"patient_id": wp2["id"], "program_code": "hypertension",
              "org_id": township["id"], "scale_code": "scr_hypertension",
              "answers": YES},
        headers=h,
    )
    s2 = client.post(
        f"{B}/screenings",
        json={"patient_id": wp3["id"], "program_code": "hypertension",
              "org_id": village["id"], "scale_code": "scr_hypertension",
              "answers": YES},
        headers=h,
    ).json()
    client.post(f"{B}/screenings/{s2['id']}/review",
                json={"review_result": "confirmed", "review_note": "复核确认"}, headers=h)
    referral = client.post(
        f"{B}/referrals",
        json={"patient_id": wp1["id"], "program_code": "wbp_a", "direction": "up",
              "target_org_id": county["id"], "reason": "血压控制不佳"},
        headers=h,
    ).json()
    assert referral["status"] == "submitted"
    client.post(
        f"{B}/measurements",
        json={"patient_id": wp1["id"], "metric": "bp_sys", "value": 120,
              "program_code": "wbp_a"},
        headers=h,
    )
    client.post(
        f"{B}/assessments",
        json={"patient_id": wp1["id"], "scale_code": "scr_hypertension", "answers": NO},
        headers=h,
    )
    client.post(
        f"{B}/data-sources",
        json={"code": "WB-HIS", "name": "契约HIS", "source_type": "HIS",
              "org_id": township["id"]},
        headers=h,
    )
    center = client.post(
        f"{B}/centers",
        json={"code": "WB-CTR", "name": "契约中心", "program_code": "wbp_a",
              "lead_org_id": county["id"], "lead_dept": "心内科",
              "org_ids": [county["id"], township["id"]], "team_ids": [team["id"]]},
        headers=h,
    ).json()
    path = client.post(
        f"{B}/path-templates",
        json={"program_id": prog_a["id"], "code": "WB-PATH", "name": "契约路径",
              "scene": "outpatient"},
        headers=h,
    ).json()
    client.post(f"{B}/path-templates/{path['id']}/nodes",
                json={"key": "n1", "name": "首次随访", "seq": 1}, headers=h)
    client.post(f"{B}/path-templates/{path['id']}/status",
                json={"status": "published"}, headers=h)
    # 随访计划：按种子方案 fr_discharge 的时间点生成，执行人=契约医生
    rules = client.get(f"{B}/followup-rules", headers=h).json()
    rule = next(r for r in rules if r["code"] == "fr_discharge")
    plan = client.post(
        f"{B}/followup-plans",
        json={"patient_id": wp1["id"], "rule_id": rule["id"],
              "base_date": date.today().isoformat(), "org_id": township["id"],
              "executor_id": doc["id"]},
        headers=h,
    ).json()
    fu_points = [int(p) for p in rule["points"]]
    assert plan["created"] == len(fu_points)
    # 考核：单指标方案对乡级机构跑一次分
    assess_plan = client.post(
        f"{B}/assess-plans",
        json={"code": "WB-PLAN", "name": "契约考核", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": "followup_rate", "weight": 100}]},
        headers=h,
    ).json()
    client.post(f"{B}/scores/run",
                json={"plan_id": assess_plan["id"], "period": "2026-08"}, headers=h)
    return {
        "county": county, "township": township, "village": village,
        "doc": doc, "vd": vd, "wp1": wp1, "wp2": wp2, "wp3": wp3,
        "team": team, "center": center, "path": path, "prog_a": prog_a,
        "e1": e1, "fu_points": fu_points,
        "doc_h": _login(client, "wb_doc", "pass123456"),
        "vd_h": _login(client, "wb_vd", "pass123456"),
    }


def _iso(value: str) -> str:
    datetime.fromisoformat(value)
    return value


ZERO_SWEEP = {"overdue": 0, "escalated": 0, "revisits": 0, "followups": 0}
ALL_TASKS = {"open": 2, "overdue": 0, "due_today": 1, "escalated": 0, "done_total": 1,
             "by_type": {"followup": 1, "assess": 1}}
ENROLL_ALL = {"enrolled": 3, "high_risk": 1, "new_this_month": 3, "archived": 0,
              "by_risk": {"mid": 1, "high": 1, "low": 1},
              "by_program": {"wbp_a": 2, "wbp_b": 1}, "by_status": {"active": 3}}
REFERRALS = {"total": 1, "open": 1, "closed": 0, "closure_rate": 0.0,
             "effective_visits": 0, "by_status": {"submitted": 1}}
ZERO_PATHS = {"total": 0, "running": 0, "completed": 0, "completion_rate": 0.0}


def _followups(points):
    return {"total": len(points), "done": 0, "completion_rate": 0.0,
            "overdue": sum(1 for p in points if p < 0),
            "abnormal": 0}


def _programs_catalog(client, h):
    return client.get(f"{B}/programs", headers=h).json()


# ------------------------------------------------- 平台管理端


def test_平台管理端工作台(client, h, wb):
    body = client.get(f"{B}/workbench/admin", headers=h).json()
    assert list(body) == ["alerts", "parallel_tracks", "config_health", "data_sources",
                          "enrollment", "tasks"]
    assert list(body["alerts"]) == ["overdue_tasks", "overdue_followups",
                                    "pending_review_screenings", "pending_applies",
                                    "pending_migrations", "swept"]
    programs = _programs_catalog(client, h)
    chronic = [p for p in programs if p["category"] == "chronic"]
    specialty = [p for p in programs if p["category"] == "specialty"]
    published_scales = client.get(f"{B}/scales",
                                  params={"status": "published", "limit": 200},
                                  headers=h).json()
    assert body == {
        "alerts": {"overdue_tasks": 0, "overdue_followups": 0,
                   "pending_review_screenings": 1, "pending_applies": 0,
                   "pending_migrations": 0, "swept": ZERO_SWEEP},
        "parallel_tracks": {
            "chronic": {"programs": len(chronic), "enrolled": 2},
            "specialty": {"programs": len(specialty), "enrolled": 1},
        },
        "config_health": {
            "programs": len(programs),
            "active_programs": sum(1 for p in programs if p["active"]),
            "programs_without_rules": [p["code"] for p in programs
                                       if not p["include_rules"]],
            "published_paths": 1, "draft_paths": 0,
            "published_scales": len(published_scales),
            "teams": 1, "village_doctors": 1,
        },
        "data_sources": {"total": 1, "failed": 0, "delayed": 0, "stale_over_24h": 1,
                         "avg_success_rate": 0.0},
        "enrollment": ENROLL_ALL,
        "tasks": ALL_TASKS,
    }
    assert isinstance(body["data_sources"]["avg_success_rate"], float)


# ------------------------------------------------- 卫健管理端


def test_卫健管理端工作台(client, h, wb):
    body = client.get(f"{B}/workbench/health-commission", headers=h).json()
    assert list(body) == ["core", "enrollment", "tasks", "followups", "referrals",
                          "paths", "by_level", "centers", "scores"]
    assert list(body["core"]) == ["registered_patients", "screened", "suspect",
                                  "candidates", "enrolled", "self_managed",
                                  "service_persons", "service_times",
                                  "screening_conversion_rate", "updated_at"]
    score_row = body["scores"][0]
    assert isinstance(score_row["total_score"], float)
    assert body == {
        "core": {"registered_patients": 3, "screened": 2, "suspect": 2,
                 "candidates": 1, "enrolled": 3, "self_managed": 1,
                 "service_persons": 1, "service_times": 1,
                 "screening_conversion_rate": 100.0,
                 "updated_at": _iso(body["core"]["updated_at"])},
        "enrollment": ENROLL_ALL,
        "tasks": ALL_TASKS,
        "followups": _followups(wb["fu_points"]),
        "referrals": REFERRALS,
        "paths": ZERO_PATHS,
        "by_level": {
            "县级": {"orgs": 1, "enrolled": 0, "teams": 0},
            "乡级": {"orgs": 1, "enrolled": 2, "teams": 1},
            "村级": {"orgs": 1, "enrolled": 1, "teams": 0},
        },
        "centers": [{"id": wb["center"]["id"], "code": "WB-CTR", "name": "契约中心",
                     "program_code": "wbp_a", "status": "running", "orgs": 2,
                     "teams": 1}],
        "scores": [{"object_name": "契约卫生院", "period": "2026-08",
                    "total_score": score_row["total_score"], "rank": 1}],
    }


# ------------------------------------------------- 区域结构分析


def test_区域慢专病结构分析(client, h, wb):
    body = client.get(f"{B}/stats/region", headers=h).json()
    assert list(body) == ["total", "by_program", "by_risk", "by_stage", "by_org",
                          "age_distribution", "gender_distribution", "referrals",
                          "paths", "followups", "measurements"]
    # by_org 的键是机构 id（int），JSON 序列化成字符串键——契约必须按 int 键建模
    assert body == {
        "total": 3,
        "by_program": {"wbp_a": 2, "wbp_b": 1},
        "by_risk": {"mid": 1, "high": 1, "low": 1},
        "by_stage": {"s1": 2, "": 1},
        "by_org": {str(wb["township"]["id"]): 2, str(wb["village"]["id"]): 1},
        "age_distribution": {"0-17": 0, "18-44": 0, "45-59": 1, "60-74": 0,
                             "75+": 1, "未知": 1},
        "gender_distribution": {"男": 1, "女": 1, "未知": 1},
        "referrals": REFERRALS,
        "paths": ZERO_PATHS,
        "followups": _followups(wb["fu_points"]),
        "measurements": {"total": 1, "normal": 1},
    }


# ------------------------------------------------- 专病专家端


def test_专病专家端工作台(client, h, wb):
    body = client.get(f"{B}/workbench/expert", params={"program_code": "wbp_a"},
                      headers=h).json()
    assert list(body) == ["programs", "centers", "enrollment", "paths", "referrals",
                          "assessments", "org_coverage"]
    assert body == {
        "programs": [{"program_code": "wbp_a", "program_name": "契约甲病",
                      "category": "chronic", "version": "v1",
                      "has_include_rules": True, "stages": 1, "path_templates": 1,
                      "published_paths": 1, "scales": 0, "enrolled": 2}],
        "centers": [{"id": wb["center"]["id"], "name": "契约中心",
                     "program_code": "wbp_a", "lead_dept": "心内科",
                     "status": "running", "version": "v1"}],
        "enrollment": {"enrolled": 2, "high_risk": 0, "new_this_month": 2,
                       "archived": 0, "by_risk": {"mid": 1, "low": 1},
                       "by_program": {"wbp_a": 2}, "by_status": {"active": 2}},
        "paths": ZERO_PATHS,
        "referrals": REFERRALS,
        "assessments": {"total": 1, "by_risk": {"low": 1}},
        "org_coverage": 2,
    }


# ------------------------------------------------- 全程管理中心端


def test_全程管理中心端工作台(client, h, wb):
    body = client.get(f"{B}/workbench/center", headers=h).json()
    assert list(body) == ["todo", "pool", "enrollment", "monthly", "referrals",
                          "lifecycle", "case_reports", "teams"]
    assert body == {
        "todo": {
            # 管理员名下没有待办；tB 办结时兜底记到了办结人（管理员）名下
            "mine": {"open": 0, "overdue": 0, "due_today": 0, "escalated": 0,
                     "done_total": 1, "by_type": {}},
            "all": ALL_TASKS,
            "unassigned": 1,
            "swept": ZERO_SWEEP,
        },
        "pool": {"suspect": 1, "target": 1, "unassigned": 1, "excluded": 0,
                 "pending_review": 1},
        "enrollment": ENROLL_ALL,
        "monthly": {"new_enrollments": 3, "done_tasks": 1},
        "referrals": REFERRALS,
        "lifecycle": {"dead": 0, "migrated": 0, "excluded": 0,
                      "pending_migrations": 0, "recalling": 0},
        "case_reports": {"pending": 0},
        "teams": 1,
    }


# ------------------------------------------------- 服务团队端（三个角色三种键序）


TEAM_BASE_KEYS = ["role", "teams", "patients", "tasks", "plans", "alerts"]


def _team_common(wb):
    due_fu = sum(1 for p in wb["fu_points"] if p <= 0)
    return {
        "teams": [{"id": wb["team"]["id"], "name": "契约团队", "level": "township",
                   "org_id": wb["township"]["id"], "program_codes": ["wbp_a"]}],
        "patients": {"managed": 1, "new_this_month": 1, "high_risk": 0,
                     "by_risk": {"mid": 1}},
        "tasks": {"open": 1, "overdue": 0, "due_today": 1, "escalated": 0,
                  "done_total": 0, "by_type": {"followup": 1}},
        "plans": {"pending_assess": 0, "pending_target": 0, "pending_path": 1,
                  "due_followups": due_fu, "due_revisits": 0},
        "alerts": {"abnormal_measure": 0, "referrals": 1, "recall": 0, "dead": 0},
    }


def test_团队工作台_成员端条件键(client, wb):
    body = client.get(f"{B}/workbench/team", params={"role": "member"},
                      headers=wb["doc_h"]).json()
    assert list(body) == TEAM_BASE_KEYS + ["interventions"]
    assert body == {"role": "member", **_team_common(wb), "interventions": 0}


def test_团队工作台_个案管理师端条件键(client, wb):
    body = client.get(f"{B}/workbench/team", params={"role": "case_manager"},
                      headers=wb["doc_h"]).json()
    assert list(body) == TEAM_BASE_KEYS + ["packages", "consults"]
    assert body == {
        "role": "case_manager", **_team_common(wb),
        "packages": {"bound": 0, "total_items": 0, "used_items": 0, "usage_rate": 0.0},
        "consults": 0,
    }
    assert isinstance(body["packages"]["usage_rate"], float)


def test_团队工作台_专家端条件键(client, wb):
    body = client.get(f"{B}/workbench/team", params={"role": "expert"},
                      headers=wb["doc_h"]).json()
    assert list(body) == TEAM_BASE_KEYS + ["team_patients", "paths"]
    # team_patients 的键是团队 id（int）→ JSON 字符串键
    assert body == {
        "role": "expert", **_team_common(wb),
        "team_patients": {str(wb["team"]["id"]): 1},
        "paths": ZERO_PATHS,
    }


# ------------------------------------------------- 医生移动端


DOCTOR_MOBILE_KEYS = ["user", "todo", "calendar", "referrals", "patients", "alerts",
                      "points", "performance"]


def test_医生移动端工作台_乡镇医生(client, wb):
    body = client.get(f"{B}/workbench/doctor-mobile", headers=wb["doc_h"]).json()
    assert list(body) == DOCTOR_MOBILE_KEYS
    assert list(body["user"]) == ["id", "name", "role", "org_id", "member_roles",
                                  "teams", "is_village_doctor", "village", "township"]
    today = date.today().isoformat()
    assert body == {
        "user": {"id": wb["doc"]["id"], "name": "契约医生", "role": "doctor",
                 "org_id": wb["township"]["id"], "member_roles": ["doctor"],
                 "teams": [{"id": wb["team"]["id"], "name": "契约团队",
                            "level": "township"}],
                 "is_village_doctor": False, "village": "", "township": ""},
        "todo": {"open": 1, "overdue": 0, "due_today": 1, "escalated": 0,
                 "done_total": 0, "by_type": {"followup": 1}},
        "calendar": {"today": today,
                     "followups": sum(1 for p in wb["fu_points"] if p == 0),
                     "revisits": 0, "tasks": 1},
        "referrals": {"pending_review": 1, "pending_accept": 0, "pending_receive": 0,
                      "mine": 0, "overdue": 0},
        "patients": {"mine": 1, "village": 0, "org": 2},
        "alerts": {"escalated_tasks": 0, "case_reports": 0, "high_risk_screenings": 1},
        "points": {"balance": 0, "earned": 0},
        "performance": None,
    }


def test_医生移动端工作台_村医身份(client, h, wb):
    body = client.get(f"{B}/workbench/doctor-mobile", headers=wb["vd_h"]).json()
    # 村医在 e1 上挂名签约，按种子积分规则得过一笔"签约"分——分值从规则现算
    sign_points = next(r["points"] for r in
                       client.get(f"{B}/point-rules", headers=h).json()
                       if r["event"] == "sign")
    assert body == {
        "user": {"id": wb["vd"]["id"], "name": "契约村医", "role": "doctor",
                 "org_id": wb["village"]["id"], "member_roles": ["village_doctor"],
                 "teams": [{"id": wb["team"]["id"], "name": "契约团队",
                            "level": "township"}],
                 "is_village_doctor": True, "village": "契村", "township": "契镇"},
        "todo": {"open": 0, "overdue": 0, "due_today": 0, "escalated": 0,
                 "done_total": 0, "by_type": {}},
        "calendar": {"today": date.today().isoformat(), "followups": 0, "revisits": 0,
                     "tasks": 0},
        "referrals": {"pending_review": 0, "pending_accept": 0, "pending_receive": 0,
                      "mine": 0, "overdue": 0},
        "patients": {"mine": 0, "village": 1, "org": 1},
        "alerts": {"escalated_tasks": 0, "case_reports": 0, "high_risk_screenings": 0},
        "points": {"balance": sign_points, "earned": sign_points},
        "performance": None,
    }


# ------------------------------------------------- 下拉目录


def test_目录聚合与配置接口同源(client, h, wb):
    body = client.get(f"{B}/catalog", headers=h).json()
    assert list(body) == ["programs", "teams", "scales", "centers", "path_templates"]
    programs = _programs_catalog(client, h)
    scales = client.get(f"{B}/scales", params={"status": "published", "limit": 200},
                        headers=h).json()
    assert body == {
        "programs": [{"code": p["code"], "name": p["name"], "category": p["category"],
                      "stages": p["stages"], "active": p["active"]} for p in programs],
        "teams": [{"id": wb["team"]["id"], "name": "契约团队", "level": "township",
                   "org_id": wb["township"]["id"]}],
        "scales": [{"id": s["id"], "code": s["code"], "name": s["name"],
                    "category": s["category"], "program_code": s["program_code"]}
                   for s in scales],
        "centers": [{"id": wb["center"]["id"], "code": "WB-CTR", "name": "契约中心",
                     "program_code": "wbp_a"}],
        "path_templates": [{"id": wb["path"]["id"], "code": "WB-PATH",
                            "name": "契约路径", "program_id": wb["prog_a"]["id"],
                            "scene": "outpatient", "status": "published"}],
    }
