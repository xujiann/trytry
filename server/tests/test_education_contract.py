"""远程医学教育 `/api/education` 二十一个待治理端点的**特征化网 + 响应契约**。

套路同 `test_quality_contract.py`：先补网钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §11）。
（courses 两个端点与 articles 两个端点此前已治理，不在本网范围。）

本簇的建模判断（都以此处的精确断言为依据）：

- `score` 系 Float 列（`training_records.score` / `training_assessments.score`）：
  整数值读回来就是 `85.0`，声明 `float` 才是原样——这与 Money 列相反
  （判断依据是列类型，不是字段名，见 docs/接口标准与治理.md 陷阱一）。
  入参侧 Pydantic `float` 也已把 `{"score": 85}` 转成 85.0，不存在 int 分支。
- 比率/均分恒 float：`round(x*100.0/n, 2)` 与兜底字面量 `0.0` 两条分支都是浮点。
- `avg_rating` 是**值可空**而非条件键：键恒在，无反馈时值为 null——
  声明 `float | None` 即可，无需 exclude_unset。
- 报名回执（id/plan_id/user_id/status 四键）与退报名回执（**没有 id**，三键）
  不同形——两个模型，不许互相注入。
- 直播审核/结束同形（id+status），申请多一个 title、回放另有 recording_url
  ——按实际键集合分模型，status 字面量照实建模（HealthArticleOut 先例）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

MATERIAL_KEY_ORDER = [
    "id", "course_id", "title", "material_type", "material_type_name",
    "url", "play_count", "attachments",
]
PLAN_KEY_ORDER = [
    "id", "title", "technique_id", "org_id", "plan_date", "capacity",
    "trainer", "status", "enrolled", "remaining",
]
LIVE_ROW_KEY_ORDER = [
    "id", "title", "speaker", "planned_at", "status", "review_comment", "recording_url",
]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    """一家机构 + 实名各角色：出参里的考核人姓名/学员姓名全部可精确断言。"""
    org = client.post(
        "/api/organizations",
        json={"name": "契约培训医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    users = {}
    ids = {}
    for username, full_name, role in [
        ("edu_dir", "洪主任", "director"),
        ("edu_doc", "郑医生", "doctor"),
        ("edu_op", "冯经办", "operator"),
    ]:
        created = client.post(
            "/api/users",
            json={
                "username": username,
                "password": "pass123456",
                "full_name": full_name,
                "role": role,
                "org_id": org["id"],
            },
            headers=admin,
        ).json()
        ids[username] = created["id"]
        users[username] = login(client, username, "pass123456")
    course = client.post(
        "/api/education/courses",
        json={"title": "契约课程", "course_type": "vod", "category": "clinical"},
        headers=admin,
    ).json()
    return {
        "org": org,
        "course": course,
        "director": users["edu_dir"],
        "doctor": users["edu_doc"],
        "operator": users["edu_op"],
        "dir_id": ids["edu_dir"],
        "doc_id": ids["edu_doc"],
        "op_id": ids["edu_op"],
    }


# ---------------------------------------------------------------- 培训考核


@pytest.fixture(scope="module")
def exams(client, base):
    cid = base["course"]["id"]
    first = client.post(
        f"/api/education/courses/{cid}/exam", json={"score": 85}, headers=base["doctor"]
    )
    assert first.status_code == 200, first.text
    retake = client.post(
        f"/api/education/courses/{cid}/exam", json={"score": 60.5}, headers=base["doctor"]
    ).json()
    failed = client.post(
        f"/api/education/courses/{cid}/exam", json={"score": 42.5}, headers=base["operator"]
    ).json()
    return {"first": first.json(), "retake": retake, "failed": failed}


def test_考核回执精确_Float列整数成绩是浮点(base, exams):
    body = exams["first"]
    assert list(body.keys()) == ["course_id", "score", "passed"]
    assert body == {"course_id": base["course"]["id"], "score": 85.0, "passed": True}
    # Float 列 + Pydantic float 入参：整数成绩恒以 85.0 呈现，契约声明 float 才是原样
    assert isinstance(body["score"], float)
    # 重考取最高分：低分重考回执仍是 85.0
    assert exams["retake"] == {"course_id": base["course"]["id"], "score": 85.0, "passed": True}
    assert exams["failed"] == {"course_id": base["course"]["id"], "score": 42.5, "passed": False}


def test_课程统计精确(client, admin, base, exams):
    resp = client.get(f"/api/education/courses/{base['course']['id']}/stats", headers=admin)
    assert list(resp.json().keys()) == ["course_id", "trainees", "passed", "pass_rate_pct"]
    assert resp.json() == {
        "course_id": base["course"]["id"],
        "trainees": 2,
        "passed": 1,
        "pass_rate_pct": 50.0,
    }
    assert isinstance(resp.json()["pass_rate_pct"], float)


def test_我的培训记录精确(client, base, exams):
    rows = client.get("/api/education/my-records", headers=base["doctor"]).json()
    assert rows == [
        {"course_id": base["course"]["id"], "title": "契约课程", "score": 85.0, "passed": True}
    ]
    assert isinstance(rows[0]["score"], float)


# ---------------------------------------------------------------- 直播申请/审核/回放/反馈


@pytest.fixture(scope="module")
def live(client, base):
    s1 = client.post(
        "/api/education/live-sessions",
        json={"title": "契约直播一", "speaker": "郑医生", "planned_at": "2026-09-15"},
        headers=base["doctor"],
    ).json()
    approved = client.post(
        f"/api/education/live-sessions/{s1['id']}/review?approve=true&comment=同意排期",
        headers=base["director"],
    ).json()
    finished = client.post(
        f"/api/education/live-sessions/{s1['id']}/finish", headers=base["operator"]
    ).json()
    recorded = client.post(
        f"/api/education/live-sessions/{s1['id']}/recording",
        json={"recording_url": "https://cdn.example/replay/1.mp4"},
        headers=base["director"],
    ).json()
    fb1 = client.post(
        f"/api/education/live-sessions/{s1['id']}/feedback",
        json={"rating": 3, "comment": "音质一般"},
        headers=base["doctor"],
    ).json()
    fb1_again = client.post(
        f"/api/education/live-sessions/{s1['id']}/feedback",
        json={"rating": 5, "comment": "回看清晰"},
        headers=base["doctor"],
    ).json()
    fb2 = client.post(
        f"/api/education/live-sessions/{s1['id']}/feedback",
        json={"rating": 4},
        headers=base["operator"],
    ).json()
    s2 = client.post(
        "/api/education/live-sessions", json={"title": "契约直播二"}, headers=base["doctor"]
    ).json()
    rejected = client.post(
        f"/api/education/live-sessions/{s2['id']}/review?approve=false&comment=主题重复",
        headers=base["director"],
    ).json()
    return {
        "s1": s1, "approved": approved, "finished": finished, "recorded": recorded,
        "fb1": fb1, "fb1_again": fb1_again, "fb2": fb2, "s2": s2, "rejected": rejected,
    }


def test_直播申请与审核回执精确(live):
    assert list(live["s1"].keys()) == ["id", "title", "status"]
    assert live["s1"] == {"id": live["s1"]["id"], "title": "契约直播一", "status": "pending"}
    assert live["approved"] == {"id": live["s1"]["id"], "status": "approved"}
    assert live["rejected"] == {"id": live["s2"]["id"], "status": "rejected"}
    assert live["finished"] == {"id": live["s1"]["id"], "status": "finished"}
    assert live["recorded"] == {
        "id": live["s1"]["id"],
        "recording_url": "https://cdn.example/replay/1.mp4",
    }


def test_直播反馈回执_覆盖分支同一条(live):
    assert list(live["fb1"].keys()) == ["id", "updated"]
    assert live["fb1"] == {"id": live["fb1"]["id"], "updated": False}
    # 同一人重复提交按覆盖：同 id、updated=true
    assert live["fb1_again"] == {"id": live["fb1"]["id"], "updated": True}
    assert live["fb2"] == {"id": live["fb2"]["id"], "updated": False}
    assert live["fb2"]["id"] != live["fb1"]["id"]


def test_直播反馈列表精确_均分与空分支(client, base, live):
    body = client.get(
        f"/api/education/live-sessions/{live['s1']['id']}/feedback", headers=base["doctor"]
    ).json()
    assert list(body.keys()) == ["session_id", "count", "avg_rating", "feedbacks"]
    assert body == {
        "session_id": live["s1"]["id"],
        "count": 2,
        "avg_rating": 4.5,
        "feedbacks": [
            {"id": live["fb2"]["id"], "user_id": base["op_id"], "rating": 4, "comment": ""},
            {"id": live["fb1"]["id"], "user_id": base["doc_id"], "rating": 5, "comment": "回看清晰"},
        ],
    }
    assert isinstance(body["avg_rating"], float)
    # 无反馈：avg_rating 是**值为 null 的恒在键**（不是条件键）
    empty = client.get(
        f"/api/education/live-sessions/{live['s2']['id']}/feedback", headers=base["doctor"]
    ).json()
    assert empty == {
        "session_id": live["s2"]["id"], "count": 0, "avg_rating": None, "feedbacks": []
    }


def test_直播列表精确形状与过滤(client, admin, live):
    rows = client.get("/api/education/live-sessions", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [LIVE_ROW_KEY_ORDER] * 2
    s1_row = {
        "id": live["s1"]["id"],
        "title": "契约直播一",
        "speaker": "郑医生",
        "planned_at": "2026-09-15",
        "status": "finished",
        "review_comment": "同意排期",
        "recording_url": "https://cdn.example/replay/1.mp4",
    }
    s2_row = {
        "id": live["s2"]["id"],
        "title": "契约直播二",
        "speaker": "",
        "planned_at": "",
        "status": "rejected",
        "review_comment": "主题重复",
        "recording_url": "",
    }
    assert rows == [s2_row, s1_row]  # id 倒序
    assert client.get("/api/education/live-sessions?status=finished", headers=admin).json() == [
        s1_row
    ]


# ---------------------------------------------------------------- 课件资源


@pytest.fixture(scope="module")
def materials(client, base):
    cid = base["course"]["id"]
    m1 = client.post(
        f"/api/education/courses/{cid}/materials",
        json={"title": "课件一", "material_type": "slide", "url": "https://cdn.example/s1.pdf"},
        headers=base["doctor"],
    ).json()
    m2 = client.post(
        f"/api/education/courses/{cid}/materials",
        json={"title": "视频一", "material_type": "video"},
        headers=base["operator"],
    ).json()
    play1 = client.post(f"/api/education/materials/{m1['id']}/play", headers=base["doctor"]).json()
    play2 = client.post(f"/api/education/materials/{m1['id']}/play", headers=base["operator"]).json()
    return {"m1": m1, "m2": m2, "play1": play1, "play2": play2}


def test_课件回执精确形状与键序(base, materials):
    body = materials["m1"]
    assert list(body.keys()) == MATERIAL_KEY_ORDER
    assert body == {
        "id": body["id"],
        "course_id": base["course"]["id"],
        "title": "课件一",
        "material_type": "slide",
        "material_type_name": "课件",
        "url": "https://cdn.example/s1.pdf",
        "play_count": 0,
        "attachments": 0,
    }
    assert materials["m2"] == {
        "id": materials["m2"]["id"],
        "course_id": base["course"]["id"],
        "title": "视频一",
        "material_type": "video",
        "material_type_name": "视频",
        "url": "",
        "play_count": 0,
        "attachments": 0,
    }


def test_点播回执与列表精确(client, admin, base, materials):
    assert materials["play1"] == {**materials["m1"], "play_count": 1}
    assert materials["play2"] == {**materials["m1"], "play_count": 2}
    rows = client.get(
        f"/api/education/courses/{base['course']['id']}/materials", headers=admin
    ).json()
    assert rows == [materials["m2"], {**materials["m1"], "play_count": 2}]  # id 倒序


def test_课件统计精确(client, admin, materials):
    body = client.get("/api/education/material-stats", headers=admin).json()
    assert list(body.keys()) == ["total_materials", "total_plays", "top"]
    assert body == {
        "total_materials": 2,
        "total_plays": 2,
        "top": [{**materials["m1"], "play_count": 2}, materials["m2"]],  # 点播量降序
    }
    assert type(body["total_plays"]) is int


# ---------------------------------------------------------------- 适宜技术实训


@pytest.fixture(scope="module")
def plans(client, base):
    p1 = client.post(
        "/api/education/training-plans",
        json={
            "title": "契约实训",
            "org_id": base["org"]["id"],
            "plan_date": "2026-09-20",
            "capacity": 2,
            "trainer": "王老师",
        },
        headers=base["director"],
    ).json()
    e1 = client.post(
        f"/api/education/training-plans/{p1['id']}/enroll", headers=base["doctor"]
    ).json()
    e2 = client.post(
        f"/api/education/training-plans/{p1['id']}/enroll", headers=base["operator"]
    ).json()
    cancelled = client.post(
        f"/api/education/training-plans/{p1['id']}/cancel-enroll", headers=base["operator"]
    ).json()
    re_enrolled = client.post(
        f"/api/education/training-plans/{p1['id']}/enroll", headers=base["operator"]
    ).json()
    a1 = client.post(
        f"/api/education/training-plans/{p1['id']}/assessments",
        json={"user_id": base["doc_id"], "score": 88, "comment": "操作规范"},
        headers=base["director"],
    ).json()
    a2 = client.post(
        f"/api/education/training-plans/{p1['id']}/assessments",
        json={"user_id": base["op_id"], "score": 45.5},
        headers=base["director"],
    ).json()
    a1_redo = client.post(
        f"/api/education/training-plans/{p1['id']}/assessments",
        json={"user_id": base["doc_id"], "score": 92.5, "comment": "复核提高"},
        headers=base["director"],
    ).json()
    p2 = client.post(
        "/api/education/training-plans",
        json={"title": "契约实训二", "org_id": base["org"]["id"], "plan_date": "2026-10-01"},
        headers=base["director"],
    ).json()
    return {
        "p1": p1, "e1": e1, "e2": e2, "cancelled": cancelled, "re_enrolled": re_enrolled,
        "a1": a1, "a2": a2, "a1_redo": a1_redo, "p2": p2,
    }


def test_实训计划回执精确形状与键序(base, plans):
    body = plans["p1"]
    assert list(body.keys()) == PLAN_KEY_ORDER
    assert body == {
        "id": body["id"],
        "title": "契约实训",
        "technique_id": None,
        "org_id": base["org"]["id"],
        "plan_date": "2026-09-20",
        "capacity": 2,
        "trainer": "王老师",
        "status": "open",
        "enrolled": 0,
        "remaining": 2,
    }
    # 缺省分支：capacity 默认 30、trainer 空串
    assert plans["p2"] == {
        "id": plans["p2"]["id"],
        "title": "契约实训二",
        "technique_id": None,
        "org_id": base["org"]["id"],
        "plan_date": "2026-10-01",
        "capacity": 30,
        "trainer": "",
        "status": "open",
        "enrolled": 0,
        "remaining": 30,
    }


def test_计划列表带报名数精确(client, admin, plans):
    rows = client.get("/api/education/training-plans", headers=admin).json()
    assert rows == [
        plans["p2"],
        {**plans["p1"], "enrolled": 2, "remaining": 0},  # 报满：2 报名（退 1 又报回）
    ]
    assert client.get("/api/education/training-plans?status=open", headers=admin).json() == rows


def test_报名与退报名回执不同形(base, plans):
    assert list(plans["e1"].keys()) == ["id", "plan_id", "user_id", "status"]
    assert plans["e1"] == {
        "id": plans["e1"]["id"],
        "plan_id": plans["p1"]["id"],
        "user_id": base["doc_id"],
        "status": "enrolled",
    }
    # 退报名回执**没有 id**（三键）——与报名回执不同形，不许互相注入
    assert list(plans["cancelled"].keys()) == ["plan_id", "user_id", "status"]
    assert plans["cancelled"] == {
        "plan_id": plans["p1"]["id"],
        "user_id": base["op_id"],
        "status": "cancelled",
    }
    # 退过再报：复用同一条报名记录（同 id）
    assert plans["re_enrolled"] == {**plans["e2"], "status": "enrolled"}
    assert plans["re_enrolled"]["id"] == plans["e2"]["id"]


def test_报名名册精确(client, admin, base, plans):
    rows = client.get(
        f"/api/education/training-plans/{plans['p1']['id']}/enrollments", headers=admin
    ).json()
    assert rows == [
        {
            "id": plans["e1"]["id"],
            "user_id": base["doc_id"],
            "username": "edu_doc",
            "full_name": "郑医生",
            "status": "enrolled",
        },
        {
            "id": plans["e2"]["id"],
            "user_id": base["op_id"],
            "username": "edu_op",
            "full_name": "冯经办",
            "status": "enrolled",
        },
    ]


def test_考核录入回执精确_Float列成绩(base, plans):
    body = plans["a1"]
    assert list(body.keys()) == ["id", "plan_id", "user_id", "score", "passed", "assessor"]
    assert body == {
        "id": body["id"],
        "plan_id": plans["p1"]["id"],
        "user_id": base["doc_id"],
        "score": 88.0,
        "passed": True,
        "assessor": "洪主任",
    }
    assert isinstance(body["score"], float)
    assert plans["a2"] == {
        "id": plans["a2"]["id"],
        "plan_id": plans["p1"]["id"],
        "user_id": base["op_id"],
        "score": 45.5,
        "passed": False,
        "assessor": "洪主任",
    }
    # 重录更新成绩：同 id 覆盖
    assert plans["a1_redo"] == {**plans["a1"], "score": 92.5}


def test_考核榜单精确_空分支兜底(client, admin, base, plans):
    body = client.get(
        f"/api/education/training-plans/{plans['p1']['id']}/assessments", headers=admin
    ).json()
    assert list(body.keys()) == ["total", "passed", "pass_rate_pct", "items"]
    assert body == {
        "total": 2,
        "passed": 1,
        "pass_rate_pct": 50.0,
        "items": [
            {
                "id": plans["a1"]["id"],
                "user_id": base["doc_id"],
                "score": 92.5,
                "passed": True,
                "comment": "复核提高",
                "assessor": "洪主任",
            },
            {
                "id": plans["a2"]["id"],
                "user_id": base["op_id"],
                "score": 45.5,
                "passed": False,
                "comment": "",
                "assessor": "洪主任",
            },
        ],
    }
    empty = client.get(
        f"/api/education/training-plans/{plans['p2']['id']}/assessments", headers=admin
    ).json()
    assert empty == {"total": 0, "passed": 0, "pass_rate_pct": 0.0, "items": []}
    assert isinstance(empty["pass_rate_pct"], float)
