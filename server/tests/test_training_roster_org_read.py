"""实训计划的报名名单与考核成绩按主办机构判可见（P0-38 第二批）。

实训计划的写接口全都按主办机构判归属：发布（`assert_org_writable`）、报名与考核录入
（`assert_obj_org_writable(plan)`，考核由办实训的机构录入）。可两个读接口什么都不看：2026-09-24 实测，
一家与谁都没有关系的新卫生院，医生 / 经办按计划号读到县里某家机构的**报名名单**（学员登录账号与姓名）
和**考核成绩榜**（逐人得分、是否合格、评语、考核人），全部 200——名单是职工名册的子集，成绩是人事考核
数据，而职工名册早就只给看本机构（`test_机构维度管理数据不可跨机构读取`）。

修法：按计划的主办机构判 `assert_org_visible`（本机构；全域角色看全县）。计划清单本身不动——
计划是公开挂出来的（主题、日期、名额），不是人事数据。

成绩榜原先连计划在不在都不查，查不到计划就回一张空榜；这一点**照旧**（不借机改成 404），
只有查得到计划时才判归属。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def training_world(client):
    """甲卫生院办了一期实训，本院一名医生报名并已录考核；乙卫生院与它毫无关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "实训名单甲卫生院"), ("b", "实训名单乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    for username, role, key in (("p038t_org_a", "doctor", "a"), ("p038t_trainee_a", "doctor", "a"),
                                ("p038t_doc_b", "doctor", "b"), ("p038t_op_b", "operator", "b")):
        r = client.post("/api/users",
                        json={"username": username, "password": "pw123456", "full_name": f"{username}姓名",
                              "role": role, "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    h = {u: _login(client, u) for u in ("p038t_org_a", "p038t_trainee_a", "p038t_doc_b", "p038t_op_b")}

    plan = client.post("/api/education/training-plans",
                       json={"title": "实训名单测试：艾灸", "org_id": orgs["a"], "plan_date": "2026-10-10",
                             "capacity": 5},
                       headers=h["p038t_org_a"])
    assert plan.status_code == 201, plan.text
    pid = plan.json()["id"]
    enrolled = client.post(f"/api/education/training-plans/{pid}/enroll", headers=h["p038t_trainee_a"])
    assert enrolled.status_code == 201, enrolled.text
    scored = client.post(f"/api/education/training-plans/{pid}/assessments",
                         json={"user_id": enrolled.json()["user_id"], "score": 58,
                               "comment": "实训名单测试评语：取穴不准"},
                         headers=h["p038t_org_a"])
    assert scored.status_code == 201, scored.text
    return {"admin": admin, "h": h, "pid": pid}


def _urls(pid):
    return {
        "报名名单": (f"/api/education/training-plans/{pid}/enrollments", "p038t_trainee_a"),
        "考核成绩": (f"/api/education/training-plans/{pid}/assessments", "实训名单测试评语"),
    }


@pytest.mark.parametrize("label", ["报名名单", "考核成绩"])
@pytest.mark.parametrize("who", ["p038t_doc_b", "p038t_op_b"])
def test_无关机构按计划号读不到名单与成绩(client, training_world, label, who):
    url, marker = _urls(training_world["pid"])[label]
    r = client.get(url, headers=training_world["h"][who])
    assert r.status_code == 403, (label, who, r.status_code, r.text)
    assert marker not in r.text


@pytest.mark.parametrize("label", ["报名名单", "考核成绩"])
@pytest.mark.parametrize("who", ["p038t_org_a", "p038t_trainee_a"])
def test_主办机构照常读得到(client, training_world, label, who):
    url, marker = _urls(training_world["pid"])[label]
    r = client.get(url, headers=training_world["h"][who])
    assert r.status_code == 200, (label, who, r.text)
    assert marker in r.text


@pytest.mark.parametrize("label", ["报名名单", "考核成绩"])
def test_全域角色照常读全县(client, training_world, label):
    url, marker = _urls(training_world["pid"])[label]
    r = client.get(url, headers=training_world["admin"])
    assert r.status_code == 200, (label, r.text)
    assert marker in r.text


def test_计划清单照旧全县可见(client, training_world):
    """计划是公开挂出来的——收口的是名单与成绩，不是计划本身。"""
    r = client.get("/api/education/training-plans", headers=training_world["h"]["p038t_doc_b"])
    assert r.status_code == 200, r.text
    assert any(p["id"] == training_world["pid"] for p in r.json())


def test_计划不存在_名单照旧404_成绩照旧回空榜(client, training_world):
    h = training_world["h"]["p038t_doc_b"]
    assert client.get("/api/education/training-plans/987654/enrollments", headers=h).status_code == 404
    r = client.get("/api/education/training-plans/987654/assessments", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 0, "passed": 0, "pass_rate_pct": 0.0, "items": []}
