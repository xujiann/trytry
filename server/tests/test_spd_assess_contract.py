"""慢专病考核域（`spd/assess`）24 个端点的**响应契约**特征化网。

本模块历史上整块缺测试（取数逻辑有 `test_spd_assess_metrics.py`，但接口出参
从未被钉住），所以这里的场景全部**经 HTTP API 种出来**，再对代表性端点断言
完整精确 JSON（dict 相等）与键序（`list(resp.json().keys())`）。

三处最要紧的判断（加 `response_model` 前后都得成立）：

1. **`/scores-analysis` 两条分支的键序不同**：无数据分支是
   `total, distribution, top_deductions, average`（没有 ranking），有数据分支是
   `total, average, distribution, top_deductions, ranking`——一个模型排不出两种
   顺序，契约用的是**二选一联合**（空分支模型 `extra="forbid"`，有 ranking 的
   进不来；满分支模型要求 ranking，空的进不来），两条分支各自按各自的声明序出。
2. **`/point-accounts/me` 无账户分支没有 `account_id`**：不是 null，是整个键
   不存在——`account_id` 声明在最前 + `exclude_unset`，两条分支同一个模型对齐。
3. **Float 列（weight/target_value/total_score）整数值读回来是 `x.0`**，与
   Integer 列（points/balance/stock）的裸 int 并存——两类都要有取值被钉住。
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


def _ts(value):
    """时间戳只钉格式不钉取值——它是本场景里唯一不可复现的字节。"""
    assert isinstance(value, str) and ISO_TS.match(value), value
    return value


@pytest.fixture(scope="module")
def world(client, auth):
    """两家机构 × 三条任务（甲院 2 条办结 1、乙院 1 条未办）+ 一次跑分。

    顺序敏感的几步（无账户快照 → 签到 → 有账户快照）在这里一次做完并留底，
    测试函数只对留底断言，谁先谁后就不再要紧。
    """
    period = date.today().strftime("%Y-%m")
    # 1) 积分账户"无账户"分支必须在一切会建账户的动作之前取样
    me_before = client.get(f"{B}/point-accounts/me", headers=auth)
    assert me_before.status_code == 200

    orgs = [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": "township", "level": "township"},
            headers=auth,
        ).json()
        for name in ("契约甲卫生院", "契约乙卫生院")
    ]
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"契约患者{i}", "id_card": f"33066619900101{i:04d}",
                  "gender": "男", "birth_date": "1990-01-01"},
            headers=auth,
        ).json()
        for i in range(3)
    ]
    tasks = []
    for patient, org in [(patients[0], orgs[0]), (patients[1], orgs[0]), (patients[2], orgs[1])]:
        resp = client.post(
            f"{B}/tasks",
            json={"patient_id": patient["id"], "title": "契约取数任务",
                  "task_type": "followup", "org_id": org["id"], "due_days": 7},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        tasks.append(resp.json())
    done = client.post(f"{B}/tasks/{tasks[0]['id']}/complete", json={}, headers=auth)
    assert done.status_code == 200, done.text

    indicator = client.post(
        f"{B}/indicators",
        json={"code": "ct_done_rate", "name": "契约完成率", "data_source": "task",
              "object_type": "org", "formula": "done / total * 100", "target_value": 100,
              "score_rule": {"type": "ratio", "full": 100, "target": 100}},
        headers=auth,
    )
    assert indicator.status_code == 201, indicator.text
    plan = client.post(
        f"{B}/assess-plans",
        json={"code": "ct_plan", "name": "契约考核", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": "ct_done_rate", "weight": 100}]},
        headers=auth,
    )
    assert plan.status_code == 201, plan.text
    run = client.post(
        f"{B}/scores/run",
        json={"plan_id": plan.json()["id"], "period": period,
              "object_ids": [o["id"] for o in orgs]},
        headers=auth,
    )
    assert run.status_code == 200, run.text

    # 2) 签到（建出账户、入 1 分）→ "有账户"分支取样
    signin = client.post(f"{B}/point-accounts/signin", headers=auth)
    assert signin.status_code == 200, signin.text
    me_after = client.get(f"{B}/point-accounts/me", headers=auth)

    return {
        "period": period, "orgs": orgs, "patients": patients,
        "me_before": me_before.json(), "me_before_keys": list(me_before.json().keys()),
        "signin": signin.json(), "signin_keys": list(signin.json().keys()),
        "me_after": me_after.json(), "me_after_keys": list(me_after.json().keys()),
        "indicator": indicator.json(), "indicator_keys": list(indicator.json().keys()),
        "plan": plan.json(), "plan_keys": list(plan.json().keys()),
        "run": run.json(), "run_keys": list(run.json().keys()),
    }


INDICATOR_KEYS = ["id", "code", "name", "program_codes", "object_type", "data_source",
                  "scope_expr", "formula", "score_rule", "weight", "target_value",
                  "abnormal_rule", "version", "effective_from", "effective_scope", "active"]
PLAN_KEYS = ["id", "code", "name", "level", "program_codes", "object_type",
             "period_type", "items", "active"]
RANK_ROW_KEYS = ["object_id", "object_name", "total_score", "rank"]


# ============================================================ 指标库


def test_指标新建回执完整精确(client, auth, world):
    created = world["indicator"]
    assert world["indicator_keys"] == INDICATOR_KEYS
    assert created == {
        "id": created["id"], "code": "ct_done_rate", "name": "契约完成率",
        "program_codes": [], "object_type": "org", "data_source": "task",
        "scope_expr": "", "formula": "done / total * 100",
        "score_rule": {"type": "ratio", "full": 100, "target": 100},
        # Float 列：整数目标值读回来是 100.0；weight 缺省 1.0
        "weight": 1.0, "target_value": 100.0, "abnormal_rule": "",
        "version": "v1", "effective_from": "", "effective_scope": "region", "active": True,
    }
    assert isinstance(created["weight"], float) and isinstance(created["target_value"], float)


def test_指标列表行与新建回执同形(client, auth, world):
    rows = client.get(f"{B}/indicators", params={"limit": 100}, headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_done_rate")
    assert list(mine.keys()) == INDICATOR_KEYS
    assert mine == world["indicator"]


def test_指标修改回执_整数权重写入读回是float(client, auth, world):
    created = client.post(
        f"{B}/indicators",
        json={"code": "ct_tmp", "name": "契约临时指标", "data_source": "task"},
        headers=auth,
    ).json()
    patched = client.patch(f"{B}/indicators/{created['id']}",
                           json={"weight": 2}, headers=auth)
    assert patched.status_code == 200
    # PATCH 塞进去的裸 int 2，经 Float 列落库重读后是 2.0——这是既有字节行为
    assert patched.json() == {**created, "weight": 2.0}
    assert isinstance(patched.json()["weight"], float)


def test_指标使用情况(client, auth, world):
    iid = world["indicator"]["id"]
    usage = client.get(f"{B}/indicators/{iid}/usage", headers=auth).json()
    assert list(usage.keys()) == ["indicator", "plans", "used_by"]
    assert usage == {
        "indicator": world["indicator"],
        # weight 取的是方案 items 里的原始 JSON 值：int 100 原样透出，不是 100.0
        "plans": [{"id": world["plan"]["id"], "code": "ct_plan", "name": "契约考核",
                   "level": "township", "weight": 100}],
        "used_by": 1,
    }
    assert list(usage["plans"][0].keys()) == ["id", "code", "name", "level", "weight"]
    assert isinstance(usage["plans"][0]["weight"], int)


# ============================================================ 考核方案


def test_方案新建列表修改(client, auth, world):
    created = world["plan"]
    assert world["plan_keys"] == PLAN_KEYS
    assert created == {
        "id": created["id"], "code": "ct_plan", "name": "契约考核", "level": "township",
        "program_codes": [], "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": "ct_done_rate", "weight": 100}], "active": True,
    }
    rows = client.get(f"{B}/assess-plans", headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_plan")
    assert list(mine.keys()) == PLAN_KEYS and mine == created

    patched = client.patch(f"{B}/assess-plans/{created['id']}",
                           json={"name": "契约考核v2"}, headers=auth)
    assert patched.status_code == 200
    assert patched.json() == {**created, "name": "契约考核v2"}
    # 改回去，别让后面的分析断言跟着变
    client.patch(f"{B}/assess-plans/{created['id']}", json={"name": "契约考核"}, headers=auth)


# ============================================================ 计分


def test_跑分回执完整精确(client, auth, world):
    a, b = world["orgs"]
    assert world["run_keys"] == ["plan", "period", "scored", "top"]
    assert world["run"] == {
        "plan": world["plan"], "period": world["period"], "scored": 2,
        "top": [
            {"object_id": a["id"], "object_name": "契约甲卫生院",
             "total_score": 50.0, "rank": 1},
            {"object_id": b["id"], "object_name": "契约乙卫生院",
             "total_score": 0.0, "rank": 2},
        ],
    }
    assert list(world["run"]["top"][0].keys()) == RANK_ROW_KEYS
    # Float 列：满未满都是 float；整数得分也带 .0
    assert isinstance(world["run"]["top"][0]["total_score"], float)
    assert isinstance(world["run"]["top"][1]["total_score"], float)


def test_结果列表行完整精确(client, auth, world):
    a, b = world["orgs"]
    rows = client.get(f"{B}/scores", params={"plan_id": world["plan"]["id"]},
                      headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "plan_id", "period", "object_type", "object_id", "object_name",
         "program_code", "total_score", "rank", "created_at"]
    ] * 2
    assert rows[0] == {
        "id": rows[0]["id"], "plan_id": world["plan"]["id"], "period": world["period"],
        "object_type": "org", "object_id": a["id"], "object_name": "契约甲卫生院",
        "program_code": "", "total_score": 50.0, "rank": 1,
        "created_at": _ts(rows[0]["created_at"]),
    }
    assert rows[1]["object_id"] == b["id"] and rows[1]["total_score"] == 0.0


def test_下钻明细完整精确(client, auth, world):
    rows = client.get(f"{B}/scores", params={"plan_id": world["plan"]["id"]},
                      headers=auth).json()
    detail = client.get(f"{B}/scores/{rows[0]['id']}", headers=auth).json()
    assert list(detail.keys()) == ["id", "plan", "period", "object_type", "object_id",
                                   "object_name", "total_score", "rank", "detail",
                                   "created_at"]
    assert detail == {
        "id": rows[0]["id"], "plan": world["plan"], "period": world["period"],
        "object_type": "org", "object_id": world["orgs"][0]["id"],
        "object_name": "契约甲卫生院", "total_score": 50.0, "rank": 1,
        "detail": [{
            "indicator_code": "ct_done_rate", "indicator_name": "契约完成率",
            "metrics": {"total": 2.0, "done": 1.0, "overdue": 0.0},
            "value": 50.0, "raw_score": 50.0, "weight": 100.0, "score": 50.0,
            "deduction": 50.0, "reason": "未达目标值100.0（实际50.0）",
            "target_value": 100.0,
        }],
        "created_at": _ts(detail["created_at"]),
    }


def test_得分分析两条分支键序不同(client, auth, world):
    """空分支连 ranking 键都没有，且 average 排在最后——这不是笔误，是当前字节。"""
    empty = client.get(f"{B}/scores-analysis",
                       params={"plan_id": 999999, "period": world["period"]},
                       headers=auth).json()
    assert list(empty.keys()) == ["total", "distribution", "top_deductions", "average"]
    assert empty == {"total": 0, "distribution": {}, "top_deductions": [], "average": 0.0}

    full = client.get(f"{B}/scores-analysis",
                      params={"plan_id": world["plan"]["id"], "period": world["period"]},
                      headers=auth).json()
    assert list(full.keys()) == ["total", "average", "distribution", "top_deductions",
                                 "ranking"]
    a, b = world["orgs"]
    assert full == {
        "total": 2, "average": 25.0,
        "distribution": {"90+": 0, "80-89": 0, "70-79": 0, "60-69": 0, "<60": 2},
        "top_deductions": [{"indicator_code": "ct_done_rate", "indicator_name": "契约完成率",
                            "count": 2, "total_deduction": 150.0}],
        "ranking": [
            {"object_id": a["id"], "object_name": "契约甲卫生院", "total_score": 50.0,
             "rank": 1},
            {"object_id": b["id"], "object_name": "契约乙卫生院", "total_score": 0.0,
             "rank": 2},
        ],
    }
    assert list(full["top_deductions"][0].keys()) == ["indicator_code", "indicator_name",
                                                      "count", "total_deduction"]


# ============================================================ 工作量


def test_工作量统计机构口径完整精确(client, auth, world):
    a, b = world["orgs"]
    body = client.get(f"{B}/workload",
                      params={"object_type": "org", "period": world["period"]},
                      headers=auth).json()
    assert list(body.keys()) == ["period", "object_type", "items"]
    assert body == {
        "period": world["period"], "object_type": "org",
        "items": [
            {"object_id": a["id"], "total": 2, "done": 1, "by_type": {"followup": 1},
             "object_name": "契约甲卫生院", "completion_rate": 50.0},
            {"object_id": b["id"], "total": 1, "done": 0, "by_type": {},
             "object_name": "契约乙卫生院", "completion_rate": 0.0},
        ],
    }
    assert list(body["items"][0].keys()) == ["object_id", "total", "done", "by_type",
                                             "object_name", "completion_rate"]


def test_工作量统计人员口径(client, auth, world):
    body = client.get(f"{B}/workload", params={"period": world["period"]},
                      headers=auth).json()
    # 只有办结那条任务有责任人（办结时兜底记到操作者 admin 名下）
    assert body == {
        "period": world["period"], "object_type": "doctor",
        "items": [{"object_id": 1, "total": 1, "done": 1, "by_type": {"followup": 1},
                   "object_name": "平台管理员", "completion_rate": 100.0}],
    }


# ============================================================ 积分账户


def test_积分账户无账户分支没有account_id键(client, auth, world):
    assert world["me_before_keys"] == ["balance", "earned", "used", "records"]
    assert world["me_before"] == {"balance": 0, "earned": 0, "used": 0, "records": []}


def test_签到回执与有账户分支完整精确(client, auth, world):
    assert world["signin_keys"] == ["points", "balance"]
    assert world["signin"] == {"points": 1, "balance": 1}
    assert isinstance(world["signin"]["balance"], int)  # Integer 列：裸 int 不是 1.0

    me = world["me_after"]
    assert world["me_after_keys"] == ["account_id", "balance", "earned", "used", "records"]
    record = me["records"][0]
    assert list(record.keys()) == ["id", "rule_code", "direction", "points",
                                   "balance_after", "note", "created_at"]
    assert me == {
        "account_id": me["account_id"], "balance": 1, "earned": 1, "used": 0,
        "records": [{"id": record["id"], "rule_code": "pt_signin", "direction": "in",
                     "points": 1, "balance_after": 1, "note": "每日签到",
                     "created_at": _ts(record["created_at"])}],
    }


def test_积分账户列表行(client, auth, world):
    rows = client.get(f"{B}/point-accounts", headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "user_id", "user_name", "org_id", "balance", "earned", "used"]
    ]
    assert rows[0] == {"id": world["me_after"]["account_id"], "user_id": 1,
                       "user_name": "平台管理员", "org_id": None,
                       "balance": 1, "earned": 1, "used": 0}


# ============================================================ 积分规则


def test_积分规则新建列表修改(client, auth, world):
    created = client.post(
        f"{B}/point-rules",
        json={"code": "ct_pr", "name": "契约积分规则", "event": "followup",
              "points": 2, "daily_limit": 10},
        headers=auth,
    )
    assert created.status_code == 201
    body = created.json()
    assert list(body.keys()) == ["id", "code", "event", "points"]
    assert body == {"id": body["id"], "code": "ct_pr", "event": "followup", "points": 2}

    rows = client.get(f"{B}/point-rules", headers=auth).json()
    seeded = next(r for r in rows if r["code"] == "pt_signin")
    assert list(seeded.keys()) == ["id", "code", "name", "event", "points",
                                   "daily_limit", "active"]
    assert seeded == {"id": seeded["id"], "code": "pt_signin", "name": "每日签到",
                      "event": "signin", "points": 1, "daily_limit": 1, "active": True}

    patched = client.patch(f"{B}/point-rules/{body['id']}", json={"points": 5},
                           headers=auth)
    assert patched.status_code == 200
    assert list(patched.json().keys()) == ["id", "points", "active"]
    assert patched.json() == {"id": body["id"], "points": 5, "active": True}


# ============================================================ 商品与兑换


def test_商品与兑换核销全链路(client, auth, world):
    created = client.post(
        f"{B}/goods",
        json={"code": "ct_gd", "name": "契约毛巾", "points": 1, "stock": 2},
        headers=auth,
    )
    assert created.status_code == 201
    goods = created.json()
    assert list(goods.keys()) == ["id", "code", "name", "stock"]
    assert goods == {"id": goods["id"], "code": "ct_gd", "name": "契约毛巾", "stock": 2}

    rows = client.get(f"{B}/goods", headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "code", "name", "points", "stock", "image_url", "active"]
    ]
    assert rows[0] == {"id": goods["id"], "code": "ct_gd", "name": "契约毛巾",
                       "points": 1, "stock": 2, "image_url": "", "active": True}

    redeemed = client.post(f"{B}/redeems", json={"goods_id": goods["id"]}, headers=auth)
    assert redeemed.status_code == 201, redeemed.text
    record = redeemed.json()
    assert list(record.keys()) == ["id", "verify_code", "balance"]
    assert re.fullmatch(r"\d{6}", record["verify_code"])
    assert record == {"id": record["id"], "verify_code": record["verify_code"],
                      "balance": 0}

    listed = client.get(f"{B}/redeems", headers=auth).json()
    assert [list(r.keys()) for r in listed] == [
        ["id", "goods_id", "goods_name", "points", "verify_code", "status",
         "created_at", "verified_at"]
    ]
    assert listed[0] == {"id": record["id"], "goods_id": goods["id"],
                         "goods_name": "契约毛巾", "points": 1,
                         "verify_code": record["verify_code"], "status": "pending",
                         "created_at": _ts(listed[0]["created_at"]),
                         "verified_at": ""}  # 未核销是空串不是 null

    verified = client.post(f"{B}/redeems/verify",
                           json={"verify_code": record["verify_code"]}, headers=auth)
    assert verified.status_code == 200
    assert list(verified.json().keys()) == ["id", "status"]
    assert verified.json() == {"id": record["id"], "status": "verified"}

    patched = client.patch(f"{B}/goods/{goods['id']}", json={"stock": 9}, headers=auth)
    assert patched.status_code == 200
    assert list(patched.json().keys()) == ["id", "stock", "active"]
    assert patched.json() == {"id": goods["id"], "stock": 9, "active": True}


# ============================================================ 错误体


def test_各类错误体都只有detail(client, auth, world):
    cases = [
        (client.patch(f"{B}/indicators/999999", json={"name": "x"}, headers=auth), 404),
        (client.post(f"{B}/indicators",
                     json={"code": "ct_done_rate", "name": "重复"}, headers=auth), 409),
        (client.post(f"{B}/indicators",
                     json={"code": "ct_bad", "name": "坏公式", "formula": "done +"},
                     headers=auth), 422),
        (client.post(f"{B}/assess-plans",
                     json={"code": "ct_empty", "name": "空方案", "items": []},
                     headers=auth), 422),
        (client.post(f"{B}/assess-plans",
                     json={"code": "ct_ghost", "name": "幽灵指标",
                           "items": [{"indicator_code": "no_such", "weight": 100}]},
                     headers=auth), 422),
        (client.post(f"{B}/assess-plans",
                     json={"code": "ct_plan", "name": "重复方案",
                           "items": [{"indicator_code": "ct_done_rate", "weight": 100}]},
                     headers=auth), 409),
        (client.patch(f"{B}/assess-plans/999999", json={"name": "x"}, headers=auth), 404),
        (client.post(f"{B}/scores/run", json={"plan_id": 999999, "period": "2026-08"},
                     headers=auth), 404),
        (client.get(f"{B}/scores/999999", headers=auth), 404),
        (client.post(f"{B}/point-accounts/signin", headers=auth), 409),  # 今日已签到
        (client.patch(f"{B}/point-rules/999999", json={"points": 1}, headers=auth), 404),
        (client.patch(f"{B}/goods/999999", json={"stock": 1}, headers=auth), 404),
        (client.post(f"{B}/redeems", json={"goods_id": 999999}, headers=auth), 404),
        # 余额在兑换用例里已清零：再兑换要 409 而不是把余额扣成负数
        (client.post(
            f"{B}/redeems",
            json={"goods_id": client.get(f"{B}/goods", headers=auth).json()[0]["id"]},
            headers=auth,
        ), 409),
        (client.post(f"{B}/redeems/verify", json={"verify_code": "000000"},
                     headers=auth), 404),
    ]
    for resp, expected in cases:
        assert resp.status_code == expected, f"{resp.request.url} -> {resp.text}"
        assert set(resp.json()) == {"detail"}
