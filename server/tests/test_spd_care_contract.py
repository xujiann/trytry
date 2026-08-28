"""慢专病服务域 `spd/care`（31 端点）的**响应契约**——加 `response_model` 前后的特征化网。

场景经 HTTP API 种出（自建病种+目标+量表，量表经发布；居民端经短信登录发起咨询），
每个端点断言**完整精确 JSON**（dict 相等），代表性端点另钉**键序**——
Pydantic 序列化按声明顺序走，键序漂了就是字节变了。

三处易错的建模判断，在这里用数据钉死：

1. `measurements.value` / `assessments.score` 是 **Float 列**：整数入参读回来是
   `160.0`，契约声明 `float` 才是原样（与平台 Money 列正相反）。dict 相等比不出
   `160 == 160.0`，所以另加 `isinstance` 断言。
2. `read_at` 是"空字符串或 ISO 串"，**不是 null**（handler 写的是 `"" if None`）。
3. 趋势的 `latest` 是**可空对象**（无数据为 null），键永远在——不是条件键。
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


@pytest.fixture(scope="module")
def base(client, h):
    """全部经 HTTP API 建数据：机构、患者、自建病种+管理目标、自建量表（发布）、纳管。"""
    org = client.post(
        "/api/organizations",
        json={"name": "契约慢专卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    p1 = client.post(
        "/api/patients",
        json={"name": "契约患者一", "id_card": "330881199001015566", "gender": "男",
              "birth_date": "1990-01-01", "phone": "13900010001"},
        headers=h,
    ).json()
    # p2 无手机号：立即短信推送对它必然 failed（原因文案钉死）
    p2 = client.post(
        "/api/patients",
        json={"name": "契约患者二", "id_card": "330881199202024477", "gender": "女",
              "birth_date": "1992-02-02"},
        headers=h,
    ).json()
    program = client.post(
        f"{B}/programs",
        json={"code": "ctc_htn", "name": "契约高血压", "category": "chronic",
              "include_rules": [{"field": "age", "op": ">=", "value": 60}],
              "stages": [{"key": "stable", "name": "稳定期"}]},
        headers=h,
    ).json()
    client.post(
        f"{B}/programs/{program['id']}/targets",
        json={"stage": "stable", "metric": "bp_sys", "metric_name": "收缩压",
              "kind": "quantitative", "target_low": 90, "target_high": 139,
              "unit": "mmHg"},
        headers=h,
    )
    scale = client.post(
        f"{B}/scales",
        json={"code": "ctc_scale", "name": "契约评估量表", "category": "risk",
              "program_code": "ctc_htn",
              "items": [
                  {"key": "q1", "title": "吸烟", "type": "single",
                   "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
                  {"key": "q2", "title": "缺乏运动", "type": "single",
                   "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
              ],
              "scoring": {"ranges": [
                  {"min": 0, "max": 1, "risk": "low", "advice": "保持现状"},
                  {"min": 2, "max": 99, "risk": "mid", "advice": "改善生活方式"},
              ]}},
        headers=h,
    ).json()
    client.post(f"{B}/scales/{scale['id']}/publish", headers=h)
    enrollment = client.post(
        f"{B}/enrollments",
        json={"patient_id": p1["id"], "program_code": "ctc_htn", "org_id": org["id"],
              "risk_level": "low"},
        headers=h,
    ).json()
    assert enrollment["stage"] == "stable"
    return {"org": org, "p1": p1, "p2": p2, "program": program, "scale": scale,
            "enrollment": enrollment}


@pytest.fixture(scope="module")
def ph(client, base):
    """居民令牌（p1）：短信验证码登录 + 实名绑定，供发起在线咨询。"""
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13900010001"}
    ).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login",
                        json={"phone": "13900010001", "code": code})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post("/api/portal/auth/realname",
                json={"name": base["p1"]["name"], "id_card": base["p1"]["id_card"]},
                headers=headers)
    return headers


def _iso(value: str) -> str:
    """校验是 ISO 日期时间串后原样返回（时间戳不可预置，取回来钉格式）。"""
    datetime.fromisoformat(value)
    return value


MEASURE_KEYS = ["id", "patient_id", "program_code", "metric", "value", "unit", "level",
                "source", "device_sn", "note", "measured_at"]


def _measure_row(mid, pid, value, level, measured_at, unit="mmHg", note=""):
    return {"id": mid, "patient_id": pid, "program_code": "ctc_htn", "metric": "bp_sys",
            "value": value, "unit": unit, "level": level, "source": "manual",
            "device_sn": "", "note": note, "measured_at": measured_at}


# ------------------------------------------------- 监测数据


def test_监测录入单条与批量(client, h, base):
    pid = base["p1"]["id"]
    day = date.today() - timedelta(days=5)
    resp = client.post(
        f"{B}/measurements",
        json={"patient_id": pid, "metric": "bp_sys", "value": 160, "unit": "mmHg",
              "program_code": "ctc_htn", "measured_at": f"{day.isoformat()}T08:30:00"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert list(body) == MEASURE_KEYS
    assert body == _measure_row(body["id"], pid, 160.0, "high",
                                f"{day.isoformat()}T08:30:00")
    # Float 列：整数入参读回来是 160.0（写成 int 契约就把字节改了）
    assert isinstance(body["value"], float)
    base["m1"] = body

    batch = client.post(
        f"{B}/measurements/batch",
        json={"items": [
            {"patient_id": pid, "metric": "bp_sys", "value": 82.5, "unit": "mmHg",
             "program_code": "ctc_htn",
             "measured_at": f"{(date.today() - timedelta(days=3)).isoformat()}T09:00:00"},
            {"patient_id": pid, "metric": "bp_sys", "value": 120.5, "unit": "mmHg",
             "program_code": "ctc_htn",
             "measured_at": f"{(date.today() - timedelta(days=1)).isoformat()}T10:00:00"},
        ]},
        headers=h,
    )
    assert batch.status_code == 200
    assert batch.json() == {"created": 2, "abnormal": 1}


def test_监测列表按时间倒序且逐字段一致(client, h, base):
    pid = base["p1"]["id"]
    m1 = base["m1"]
    rows = client.get(f"{B}/measurements", params={"patient_id": pid}, headers=h).json()
    d3 = (date.today() - timedelta(days=3)).isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    assert [list(r) for r in rows] == [MEASURE_KEYS] * 3
    assert rows == [
        _measure_row(m1["id"] + 2, pid, 120.5, "normal", f"{d1}T10:00:00"),
        _measure_row(m1["id"] + 1, pid, 82.5, "low", f"{d3}T09:00:00"),
        m1,
    ]
    filtered = client.get(f"{B}/measurements",
                          params={"patient_id": pid, "level": "high"}, headers=h).json()
    assert filtered == [m1]


def test_监测趋势聚合(client, h, base):
    pid = base["p1"]["id"]
    d5 = (date.today() - timedelta(days=5)).isoformat()
    d3 = (date.today() - timedelta(days=3)).isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    body = client.get(f"{B}/measurements/trend",
                      params={"patient_id": pid, "metric": "bp_sys"}, headers=h).json()
    assert list(body) == ["metric", "granularity", "points", "level_distribution",
                          "total", "latest"]
    assert body == {
        "metric": "bp_sys",
        "granularity": "day",
        "points": [
            {"label": d5, "avg": 160.0, "min": 160.0, "max": 160.0, "count": 1},
            {"label": d3, "avg": 82.5, "min": 82.5, "max": 82.5, "count": 1},
            {"label": d1, "avg": 120.5, "min": 120.5, "max": 120.5, "count": 1},
        ],
        "level_distribution": {"high": 1, "low": 1, "normal": 1},
        "total": 3,
        "latest": _measure_row(base["m1"]["id"] + 2, pid, 120.5, "normal",
                               f"{d1}T10:00:00"),
    }
    assert all(isinstance(p["avg"], float) for p in body["points"])
    # 无数据时 latest 是 null（键仍在，不是条件键）
    empty = client.get(f"{B}/measurements/trend",
                       params={"patient_id": pid, "metric": "nope"}, headers=h).json()
    assert empty == {"metric": "nope", "granularity": "day", "points": [],
                     "level_distribution": {}, "total": 0, "latest": None}


# ------------------------------------------------- 评估


ASSESS_KEYS = ["id", "patient_id", "patient_name", "scale_id", "scale_code",
               "scale_version", "program_code", "answers", "score", "risk_level",
               "advice", "channel", "created_at"]


def test_评估开展列表与统计(client, h, base):
    pid = base["p1"]["id"]
    resp = client.post(
        f"{B}/assessments",
        json={"patient_id": pid, "scale_code": "ctc_scale",
              "answers": {"q1": "是", "q2": "是"}},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert list(body) == ASSESS_KEYS
    assert body == {
        "id": body["id"], "patient_id": pid, "patient_name": "契约患者一",
        "scale_id": base["scale"]["id"], "scale_code": "ctc_scale",
        "scale_version": "v1", "program_code": "ctc_htn",
        "answers": {"q1": "是", "q2": "是"}, "score": 3.0, "risk_level": "mid",
        "advice": "改善生活方式", "channel": "doctor", "created_at": _iso(body["created_at"]),
    }
    # Float 列：3 分读回来是 3.0
    assert isinstance(body["score"], float)

    rows = client.get(f"{B}/assessments", params={"patient_id": pid}, headers=h).json()
    assert rows == [body]

    stats = client.get(f"{B}/assessments/stats",
                       params={"scale_code": "ctc_scale"}, headers=h).json()
    assert stats == {
        "persons": 1, "times": 1, "by_risk": {"mid": 1},
        "by_item": {"q1": {"是": 1}, "q2": {"是": 1}},
    }


# ------------------------------------------------- 干预


INTERVENTION_KEYS = ["id", "patient_id", "patient_name", "enrollment_id", "program_code",
                     "template_id", "goal", "content", "measures", "frequency", "next_at",
                     "owner_id", "status", "feedback", "read_at", "created_at"]


def test_干预模板新建与列表(client, h, base):
    resp = client.post(
        f"{B}/intervention-templates",
        json={"code": "ctc_diet", "name": "契约饮食干预", "program_code": "ctc_htn",
              "category": "diet", "content": "低盐饮食", "measures": "每日盐摄入<5g",
              "frequency": "每日", "cycle_days": 30},
        headers=h,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"id": body["id"], "code": "ctc_diet", "name": "契约饮食干预"}
    base["tpl"] = body

    rows = client.get(f"{B}/intervention-templates", headers=h).json()
    assert rows == [{
        "id": body["id"], "code": "ctc_diet", "name": "契约饮食干预",
        "program_code": "ctc_htn", "category": "diet", "content": "低盐饮食",
        "measures": "每日盐摄入<5g", "frequency": "每日", "cycle_days": 30,
        "auto_risk_level": "",
    }]


def test_干预新建列表与办理(client, h, base):
    p1, p2 = base["p1"]["id"], base["p2"]["id"]
    created = client.post(
        f"{B}/interventions",
        json={"patient_ids": [p1], "program_code": "ctc_htn",
              "template_id": base["tpl"]["id"]},
        headers=h,
    )
    assert created.status_code == 201
    i1 = created.json()["ids"][0]
    assert created.json() == {"created": 1, "ids": [i1]}
    # 第二条：不引用模板、显式字段、患者未纳管（enrollment_id 为 null）
    next_at = (date.today() + timedelta(days=10)).isoformat()
    second = client.post(
        f"{B}/interventions",
        json={"patient_ids": [p2], "goal": "控制体重", "content": "每周运动150分钟",
              "measures": "快走", "frequency": "每周3次", "next_at": next_at,
              "create_task": False},
        headers=h,
    ).json()
    i2 = second["ids"][0]

    rows = client.get(f"{B}/interventions", headers=h).json()
    assert [list(r) for r in rows] == [INTERVENTION_KEYS] * 2
    assert rows == [
        {"id": i2, "patient_id": p2, "patient_name": "契约患者二",
         "enrollment_id": None, "program_code": "", "template_id": None,
         "goal": "控制体重", "content": "每周运动150分钟", "measures": "快走",
         "frequency": "每周3次", "next_at": next_at, "owner_id": rows[0]["owner_id"],
         "status": "planned", "feedback": "", "read_at": "",
         "created_at": _iso(rows[0]["created_at"])},
        {"id": i1, "patient_id": p1, "patient_name": "契约患者一",
         "enrollment_id": base["enrollment"]["id"], "program_code": "ctc_htn",
         "template_id": base["tpl"]["id"], "goal": "契约饮食干预", "content": "低盐饮食",
         "measures": "每日盐摄入<5g", "frequency": "每日",
         "next_at": (date.today() + timedelta(days=30)).isoformat(),
         "owner_id": rows[1]["owner_id"], "status": "planned", "feedback": "",
         "read_at": "", "created_at": _iso(rows[1]["created_at"])},
    ]

    patched = client.patch(f"{B}/interventions/{i1}",
                           json={"status": "doing", "feedback": "已开始执行"}, headers=h)
    assert patched.status_code == 200
    row = next(r for r in rows if r["id"] == i1)
    assert patched.json() == {**row, "patient_name": "", "status": "doing",
                              "feedback": "已开始执行"}


# ------------------------------------------------- 宣教推送


EDU_PUSH_KEYS = ["id", "material_id", "title", "patient_id", "channel", "send_at",
                 "frequency", "status", "fail_reason", "read_at", "created_at"]


def test_宣教推送即时与定时(client, h, base):
    p1, p2 = base["p1"]["id"], base["p2"]["id"]
    material = client.post(
        f"{B}/edu-materials",
        json={"code": "ctc_edu", "title": "限盐宣教", "program_code": "ctc_htn",
              "media_type": "text", "content": "每日盐摄入不超过5克"},
        headers=h,
    ).json()
    base["material"] = material

    # 立即推送：p1 有手机号走通短信通道，p2 没有 → failed（原因文案钉死）
    now_push = client.post(
        f"{B}/edu-pushes",
        json={"material_id": material["id"], "patient_ids": [p1, p2], "channel": "sms"},
        headers=h,
    )
    assert now_push.status_code == 201
    assert now_push.json() == {"pushed": 2, "sent": 1, "failed": 1, "material": "限盐宣教"}

    later = client.post(
        f"{B}/edu-pushes",
        json={"material_id": material["id"], "patient_ids": [p1], "channel": "sms",
              "send_at": "2030-01-01 08:00:00"},
        headers=h,
    )
    assert later.json() == {"pushed": 1, "sent": 0, "failed": 0, "material": "限盐宣教"}

    rows = client.get(f"{B}/edu-pushes", params={"patient_id": p2}, headers=h).json()
    assert [list(r) for r in rows] == [EDU_PUSH_KEYS]
    assert rows == [{
        "id": rows[0]["id"], "material_id": material["id"], "title": "限盐宣教",
        "patient_id": p2, "channel": "sms", "send_at": rows[0]["send_at"],
        "frequency": "once", "status": "failed", "fail_reason": "患者档案没有手机号",
        "read_at": "", "created_at": _iso(rows[0]["created_at"]),
    }]
    scheduled = client.get(f"{B}/edu-pushes", params={"status": "pending"}, headers=h).json()
    assert scheduled[0]["send_at"] == "2030-01-01 08:00:00"


def test_宣教成效统计(client, h, base):
    stats = client.get(f"{B}/edu-pushes/stats",
                       params={"program_code": "ctc_htn"}, headers=h).json()
    assert stats == {
        "covered_patients": 2, "push_times": 3, "sent": 1, "read": 0,
        "read_rate": 0.0, "by_channel": {"sms": 3},
    }
    assert isinstance(stats["read_rate"], float)


# ------------------------------------------------- 复诊计划


REVISIT_KEYS = ["id", "patient_id", "patient_name", "program_code", "plan_date", "dept",
                "doctor_user_id", "items", "source", "status", "remind_status",
                "actual_date", "log"]


def test_复诊计划新建列表与办理(client, h, base):
    pid = base["p1"]["id"]
    plan_date = (date.today() + timedelta(days=30)).isoformat()
    created = client.post(
        f"{B}/revisits",
        json={"patient_id": pid, "program_code": "ctc_htn", "plan_date": plan_date,
              "dept": "心内科", "items": "复查血压+血脂"},
        headers=h,
    )
    assert created.status_code == 201
    rid = created.json()["id"]
    assert list(created.json()) == REVISIT_KEYS
    assert created.json() == {
        "id": rid, "patient_id": pid, "patient_name": "", "program_code": "ctc_htn",
        "plan_date": plan_date, "dept": "心内科", "doctor_user_id": None,
        "items": "复查血压+血脂", "source": "manual", "status": "planned",
        "remind_status": "none", "actual_date": "", "log": [],
    }

    rows = client.get(f"{B}/revisits", params={"patient_id": pid}, headers=h).json()
    assert rows == [{**created.json(), "patient_name": "契约患者一"}]

    done = client.patch(
        f"{B}/revisits/{rid}",
        json={"status": "done", "actual_date": date.today().isoformat(), "note": "已复诊"},
        headers=h,
    ).json()
    assert done == {
        **created.json(), "status": "done", "actual_date": date.today().isoformat(),
        "log": [{"at": date.today().isoformat(), "note": "已复诊"}],
    }


# ------------------------------------------------- 上报任务与记录


CASE_REPORT_KEYS = ["id", "task_id", "patient_id", "patient_name", "program_code",
                    "report_type", "content", "trigger_rule", "status", "handle_note",
                    "created_at"]


def test_上报任务定义三端点(client, h, base):
    created = client.post(
        f"{B}/case-report-tasks",
        json={"code": "ctc_report", "name": "契约异常上报", "program_code": "ctc_htn",
              "dept": "公卫科"},
        headers=h,
    )
    assert created.status_code == 201
    tid = created.json()["id"]
    assert created.json() == {"id": tid, "code": "ctc_report", "name": "契约异常上报",
                              "active": True}
    base["report_task"] = created.json()

    rows = client.get(f"{B}/case-report-tasks", headers=h).json()
    assert rows == [{
        "id": tid, "code": "ctc_report", "name": "契约异常上报",
        "program_code": "ctc_htn", "dept": "公卫科", "manager_user_id": None,
        "assignee_ids": [], "org_ids": [], "active": True,
    }]

    patched = client.patch(f"{B}/case-report-tasks/{tid}",
                           json={"name": "契约异常上报v2"}, headers=h)
    assert patched.json() == {"id": tid, "name": "契约异常上报v2", "active": True}


def test_上报明细创建筛选与处置(client, h, base):
    pid = base["p1"]["id"]
    created = client.post(
        f"{B}/case-reports",
        json={"patient_id": pid, "task_id": base["report_task"]["id"],
              "program_code": "ctc_htn", "report_type": "review",
              "content": "血压持续偏高", "trigger_rule": "bp_sys>140x3"},
        headers=h,
    )
    assert created.status_code == 201
    rep_id = created.json()["id"]
    assert created.json() == {"id": rep_id, "status": "pending"}

    rows = client.get(f"{B}/case-reports", headers=h).json()
    assert [list(r) for r in rows] == [CASE_REPORT_KEYS]
    expected = {
        "id": rep_id, "task_id": base["report_task"]["id"], "patient_id": pid,
        "patient_name": "契约患者一", "program_code": "ctc_htn", "report_type": "review",
        "content": "血压持续偏高", "trigger_rule": "bp_sys>140x3", "status": "pending",
        "handle_note": "", "created_at": _iso(rows[0]["created_at"]),
    }
    assert rows == [expected]
    # 证件号过滤（PII 关态走 contains）
    assert client.get(f"{B}/case-reports", params={"id_card": "330881199001"},
                      headers=h).json() == [expected]
    assert client.get(f"{B}/case-reports", params={"id_card": "999999"},
                      headers=h).json() == []

    handled = client.post(f"{B}/case-reports/{rep_id}/handle",
                          json={"status": "done", "handle_note": "已随访处置"}, headers=h)
    assert handled.json() == {"id": rep_id, "status": "done"}


# ------------------------------------------------- 健康处方


def test_健康处方开具与列表(client, h, base):
    pid = base["p1"]["id"]
    created = client.post(
        f"{B}/health-prescriptions",
        json={"patient_id": pid, "program_code": "ctc_htn", "drug_advice": "按时服药",
              "life_advice": "少盐少油", "target_note": "血压<140/90"},
        headers=h,
    )
    assert created.status_code == 201
    body = created.json()
    assert body == {"id": body["id"], "created_at": _iso(body["created_at"])}

    rows = client.get(f"{B}/health-prescriptions", params={"patient_id": pid},
                      headers=h).json()
    assert rows == [{
        "id": body["id"], "program_code": "ctc_htn", "drug_advice": "按时服药",
        "rehab_advice": "", "life_advice": "少盐少油", "target_note": "血压<140/90",
        "doctor_id": rows[0]["doctor_id"], "created_at": body["created_at"],
    }]
    assert isinstance(rows[0]["doctor_id"], int)


# ------------------------------------------------- 在线咨询


CONSULT_KEYS = ["id", "patient_id", "patient_name", "program_code", "doctor_id",
                "status", "messages", "created_at"]


def test_在线咨询会话列表消息与闭环(client, h, base, ph):
    pid = base["p1"]["id"]
    started = client.post("/api/portal/spd/consults",
                          json={"program_code": "ctc_htn", "content": "血压高怎么办"},
                          headers=ph).json()
    cid = started["consult_id"]

    rows = client.get(f"{B}/consults", headers=h).json()
    assert [list(r) for r in rows] == [CONSULT_KEYS]
    assert rows == [{
        "id": cid, "patient_id": pid, "patient_name": "契约患者一",
        "program_code": "ctc_htn", "doctor_id": None, "status": "open", "messages": 1,
        "created_at": _iso(rows[0]["created_at"]),
    }]

    msgs = client.get(f"{B}/consults/{cid}/messages", headers=h).json()
    assert [list(m) for m in msgs] == [["id", "sender", "sender_id", "content",
                                        "created_at"]]
    assert msgs == [{"id": msgs[0]["id"], "sender": "patient",
                     "sender_id": msgs[0]["sender_id"], "content": "血压高怎么办",
                     "created_at": _iso(msgs[0]["created_at"])}]

    reply = client.post(f"{B}/consults/{cid}/reply",
                        json={"content": "建议规律服药并监测"}, headers=h)
    assert reply.status_code == 200
    assert reply.json() == {"id": reply.json()["id"],
                            "created_at": _iso(reply.json()["created_at"])}
    both = client.get(f"{B}/consults/{cid}/messages", headers=h).json()
    assert [m["sender"] for m in both] == ["patient", "doctor"]

    followup = client.post(f"{B}/consults/{cid}/to-followup",
                           json={"title": "咨询转随访", "due_days": 5}, headers=h)
    assert followup.json() == {
        "task_id": followup.json()["task_id"],
        "due_date": (date.today() + timedelta(days=5)).isoformat(),
    }

    closed = client.post(f"{B}/consults/{cid}/close", headers=h)
    assert closed.json() == {"id": cid, "status": "closed"}
    # 接管后列表里的 doctor_id 不再为 null（首次回复即认领）
    after = client.get(f"{B}/consults", params={"status": "closed"}, headers=h).json()
    assert after[0]["doctor_id"] is not None and after[0]["messages"] == 2


# ------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, h, base):
    cases = [
        client.post(f"{B}/assessments", headers=h,
                    json={"patient_id": base["p1"]["id"], "scale_code": "nope"}),
        client.post(f"{B}/intervention-templates", headers=h,
                    json={"code": "ctc_diet", "name": "重复编码"}),
        client.post(f"{B}/interventions", headers=h,
                    json={"patient_ids": [base["p1"]["id"]]}),
        client.patch(f"{B}/interventions/999999", headers=h, json={"status": "done"}),
        client.patch(f"{B}/revisits/999999", headers=h, json={"status": "done"}),
        client.post(f"{B}/edu-pushes", headers=h,
                    json={"material_id": 999999, "patient_ids": [base["p1"]["id"]]}),
        client.patch(f"{B}/case-report-tasks/999999", headers=h, json={"name": "x"}),
        client.post(f"{B}/case-reports/999999/handle", headers=h,
                    json={"status": "done"}),
        client.post(f"{B}/health-prescriptions", headers=h,
                    json={"patient_id": base["p1"]["id"]}),
        client.post(f"{B}/consults/999999/reply", headers=h, json={"content": "x"}),
        client.post(f"{B}/consults/999999/close", headers=h),
        client.post(f"{B}/consults/999999/to-followup", headers=h, json={}),
    ]
    assert [r.status_code for r in cases] == [404, 409, 422, 404, 404, 404,
                                              404, 404, 422, 404, 404, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
