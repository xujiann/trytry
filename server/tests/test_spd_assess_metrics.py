"""考核取数与积分兑换：八种取数口径、批量与单对象同源、扣分依据、库存与每日上限。

这里守的是**数字怎么来的**。考核结果要进绩效，取数错了不会有人报错——
只会有人拿着一份错的排名去开会。所以每种 `data_source` 都要有一条用例
钉住"喂进去什么数据、应该算出什么数字"。

另一条同样重要：`collect_metrics`（单对象，报告段落与下钻在用）必须与
`collect_metrics_batch`（整表计分在用）算出完全一样的数——它们同源是设计要求
（前者委托后者），但"设计上同源"和"真的同源"之间要有一条用例。
"""
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


@pytest.fixture(scope="module")
def world(client, h):
    """两家机构、各自的患者与业务数据——按对象分桶算得对不对，一家机构测不出来。"""
    orgs = [
        client.post(
            "/api/organizations",
            json={"name": f"取数域卫生院{i}", "org_type": "township", "level": "township"},
            headers=h,
        ).json()
        for i in (1, 2)
    ]
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"取数患者{i}", "id_card": f"33066619860606{i:04d}",
                  "gender": "男", "birth_date": "1986-06-06"},
            headers=h,
        ).json()
        for i in range(4)
    ]
    # 甲院纳管两人、乙院纳管一人；甲院其中一人做过评估
    enrollments = []
    for index, patient in enumerate(patients[:3]):
        org = orgs[0] if index < 2 else orgs[1]
        enrollments.append(client.post(
            "/api/spd/enrollments",
            json={"patient_id": patient["id"], "program_code": "hypertension",
                  "org_id": org["id"], "risk_level": "high" if index == 0 else "low"},
            headers=h,
        ).json())
    client.post(
        "/api/spd/assessments",
        json={"patient_id": patients[0]["id"], "scale_code": "assess_risk_common",
              "program_code": "hypertension",
              "answers": {"control": "达标", "adherence": "良好",
                          "complication": "无", "selfcare": "能自理"}},
        headers=h,
    )
    # 甲院两条任务（一条办结）、乙院一条
    for index, (patient, org) in enumerate(
        [(patients[0], orgs[0]), (patients[1], orgs[0]), (patients[2], orgs[1])]
    ):
        task = client.post(
            "/api/spd/tasks",
            json={"patient_id": patient["id"], "title": f"取数任务{index}",
                  "task_type": "followup", "org_id": org["id"], "due_days": 7},
            headers=h,
        ).json()
        if index == 0:
            client.post(f"/api/spd/tasks/{task['id']}/complete",
                        json={"result": {"note": "已办"}}, headers=h)
    # 甲院一条监测（异常）
    client.post(
        "/api/spd/measurements",
        json={"patient_id": patients[0]["id"], "metric": "bp_sys", "value": 168,
              "unit": "mmHg", "program_code": "hypertension"},
        headers=h,
    )
    return {"orgs": orgs, "patients": patients, "enrollments": enrollments}


def _period(client, h):
    from datetime import date

    return date.today().strftime("%Y-%m")


def _indicator(client, h, code, data_source, **kw):
    body = {"code": code, "name": f"{code} 指标", "data_source": data_source,
            "object_type": "org", **kw}
    resp = client.post("/api/spd/indicators", json=body, headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ============================================================ 八种取数口径


@pytest.mark.parametrize("data_source,expect_keys", [
    ("task", {"total", "done", "overdue"}),
    ("enrollment", {"enrolled", "target", "high_risk"}),
    ("path", {"total", "completed", "running"}),
    ("referral", {"total", "closed", "effective"}),
    ("measurement", {"total", "normal", "abnormal"}),
    ("assessment", {"assessed", "enrolled"}),
    ("archive", {"archived", "enrolled"}),
    ("case_report", {"reported", "handled"}),
])
def test_每种取数口径都给全它承诺的变量(client, h, world, data_source, expect_keys):
    """公式只能引用这些名字——少给一个，引用它的公式就会在跑分时炸。"""
    from app.database import SessionLocal
    from app.spd.routers.assess import collect_metrics_batch

    indicator = _indicator(client, h, f"probe_{data_source}", data_source)
    org_ids = [o["id"] for o in world["orgs"]]
    with SessionLocal() as db:
        from app.spd.models import SpdIndicator

        obj = db.query(SpdIndicator).filter(SpdIndicator.code == indicator["code"]).first()
        result = collect_metrics_batch(db, obj, "org", org_ids, _period(client, h))
    assert set(result) == set(org_ids), "每个考核对象都要有一份数，缺的那家会被当成 0 分"
    for org_id in org_ids:
        assert expect_keys <= set(result[org_id]), f"{data_source} 少给了变量"


def test_按机构分桶而不是把全域数字发给每一家(client, h, world):
    """甲院两条任务、乙院一条——这是最容易写错成"两家都看到 3"的地方。"""
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator
    from app.spd.routers.assess import collect_metrics_batch

    _indicator(client, h, "probe_bucket", "task")
    first, second = (o["id"] for o in world["orgs"])
    # 期望值从接口现取而不是写死：评估等动作会派生任务，写死的数字会随业务链变化而假红
    def counts(org_id):
        rows = client.get(f"/api/spd/tasks?org_id={org_id}&limit=200", headers=h).json()
        return len(rows), sum(1 for r in rows if r["status"] == "done")

    with SessionLocal() as db:
        obj = db.query(SpdIndicator).filter(SpdIndicator.code == "probe_bucket").first()
        result = collect_metrics_batch(db, obj, "org", [first, second], _period(client, h))
    for org_id in (first, second):
        total, done = counts(org_id)
        assert (result[org_id]["total"], result[org_id]["done"]) == (total, done), (
            f"机构 {org_id} 的取数与该机构任务清单对不上"
        )
    assert result[first]["total"] != result[second]["total"], (
        "两家机构拿到同一个数——多半是没按对象分桶，把全域数字发给了每一家"
    )


def test_单对象取数与批量取数逐个变量相等(client, h, world):
    """报告段落走单对象、整表计分走批量——两者算出不同的数就是"报表和考核对不上"。"""
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator
    from app.spd.routers.assess import collect_metrics, collect_metrics_batch

    period = _period(client, h)
    org_ids = [o["id"] for o in world["orgs"]]
    with SessionLocal() as db:
        for source in ("task", "enrollment", "path", "referral", "measurement",
                       "assessment", "archive", "case_report"):
            code = f"probe_{source}"
            obj = db.query(SpdIndicator).filter(SpdIndicator.code == code).first()
            if obj is None:
                continue
            batch = collect_metrics_batch(db, obj, "org", org_ids, period)
            for org_id in org_ids:
                single = collect_metrics(db, obj, "org", org_id, period)
                assert single == batch[org_id], (
                    f"{source} 口径分叉：单对象 {single} vs 批量 {batch[org_id]}"
                )


# ============================================================ 计分与扣分依据


def test_比例规则未达标按比例给分并写明理由(client, h, world):
    indicator = _indicator(
        client, h, "ratio_rule", "task",
        formula="done / total * 100", target_value=100,
        score_rule={"type": "ratio", "full": 100, "target": 100},
    )
    plan = client.post(
        "/api/spd/assess-plans",
        json={"code": "plan_ratio", "name": "比例考核", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": indicator["code"], "weight": 100}]},
        headers=h,
    ).json()
    result = client.post(
        "/api/spd/scores/run",
        json={"plan_id": plan["id"], "period": _period(client, h),
              "object_ids": [world["orgs"][0]["id"]]},
        headers=h,
    ).json()
    assert result["scored"] == 1
    score_id = client.get(f"/api/spd/scores?plan_id={plan['id']}", headers=h).json()[0]["id"]
    detail = client.get(f"/api/spd/scores/{score_id}", headers=h).json()
    item = detail["detail"][0]
    metrics = item["metrics"]
    expected = round(metrics["done"] / metrics["total"] * 100, 2)
    assert item["value"] == expected, "指标值必须等于公式对取数的求值"
    assert 0 < item["value"] < 100, "本例刻意造成未达标，好验扣分依据"
    assert item["raw_score"] == item["value"] and item["deduction"] > 0
    assert "未达目标值" in item["reason"], "扣了分必须说得出为什么"


def test_分档规则落档给分(client, h, world):
    indicator = _indicator(
        client, h, "step_rule", "task", formula="done",
        score_rule={"type": "step", "steps": [
            {"min": 2, "score": 100, "reason": ""},
            {"min": 1, "max": 1, "score": 60, "reason": "办结数偏低"},
            {"max": 0, "score": 0, "reason": "一条都没办"},
        ]},
    )
    plan = client.post(
        "/api/spd/assess-plans",
        json={"code": "plan_step", "name": "分档考核", "object_type": "org",
              "items": [{"indicator_code": indicator["code"], "weight": 100}]},
        headers=h,
    ).json()
    client.post("/api/spd/scores/run",
                json={"plan_id": plan["id"], "period": _period(client, h),
                      "object_ids": [o["id"] for o in world["orgs"]]},
                headers=h)
    scores = {s["object_id"]: s for s in
              client.get(f"/api/spd/scores?plan_id={plan['id']}", headers=h).json()}
    first, second = (o["id"] for o in world["orgs"])
    assert scores[first]["total_score"] == 60.0, "甲院办结 1 条，落 60 分那档"
    assert scores[second]["total_score"] == 0.0, "乙院一条没办，落 0 分那档"
    assert scores[first]["rank"] == 1 and scores[second]["rank"] == 2


def test_重跑同周期覆盖而不是堆两份(client, h, world):
    plan = client.get("/api/spd/assess-plans", headers=h).json()[0]
    period = _period(client, h)
    org_id = world["orgs"][0]["id"]
    for _ in range(2):
        client.post("/api/spd/scores/run",
                    json={"plan_id": plan["id"], "period": period, "object_ids": [org_id]},
                    headers=h)
    rows = [
        s for s in client.get(f"/api/spd/scores?plan_id={plan['id']}", headers=h).json()
        if s["object_id"] == org_id and s["period"] == period
    ]
    assert len(rows) == 1, "同一方案同一周期同一对象只该有一份结果"


def test_指标停用后方案里的那一项报错而不是静默算0(client, h, world):
    indicator = _indicator(client, h, "will_disable", "task", formula="done")
    plan = client.post(
        "/api/spd/assess-plans",
        json={"code": "plan_disabled", "name": "含停用指标", "object_type": "org",
              "items": [{"indicator_code": "will_disable", "weight": 100}]},
        headers=h,
    ).json()
    client.patch(f"/api/spd/indicators/{indicator['id']}", json={"active": False}, headers=h)
    client.post("/api/spd/scores/run",
                json={"plan_id": plan["id"], "period": _period(client, h),
                      "object_ids": [world["orgs"][0]["id"]]},
                headers=h)
    score_id = client.get(f"/api/spd/scores?plan_id={plan['id']}", headers=h).json()[0]["id"]
    detail = client.get(f"/api/spd/scores/{score_id}", headers=h).json()
    assert detail["detail"][0]["error"], "停用的指标要在明细里说清楚，不能当成 0 分混过去"


def test_指标被方案引用时看得出来(client, h):
    """改指标前要知道会影响哪些方案——否则改一个口径会静默改掉几张考核表。"""
    indicator = client.get("/api/spd/indicators?limit=100", headers=h).json()
    target = next(i for i in indicator if i["code"] == "ratio_rule")
    usage = client.get(f"/api/spd/indicators/{target['id']}/usage", headers=h).json()
    assert usage["used_by"] >= 1
    assert any(p["code"] == "plan_ratio" for p in usage["plans"])


# ============================================================ 积分与兑换


def test_兑换扣积分扣库存并出核销码(client, h):
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount

    goods = client.post(
        "/api/spd/goods",
        json={"code": "gd_towel", "name": "毛巾", "points": 30, "stock": 1},
        headers=h,
    ).json()
    with SessionLocal() as db:  # 直接给 admin 账户塞点积分，省掉一整条业务链
        account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == 1).first()
        if account is None:  # 唯一索引已在库上成立（P0-1），这里必须先查后建
            account = SpdPointAccount(user_id=1, balance=0, earned=0, used=0)
            db.add(account)
        account.balance += 100
        account.earned += 100
        db.commit()
        balance_before = account.balance

    redeemed = client.post("/api/spd/redeems", json={"goods_id": goods["id"]}, headers=h)
    assert redeemed.status_code == 201, redeemed.text
    record = redeemed.json()
    assert record["verify_code"], "兑换要出核销码，否则线下无从核销"

    account = client.get("/api/spd/point-accounts/me", headers=h).json()
    assert account["balance"] == balance_before - 30, "兑换要扣掉商品分值"

    # 库存已空：第二次兑换必须被拦下，而不是把库存扣成 -1
    again = client.post("/api/spd/redeems", json={"goods_id": goods["id"]}, headers=h)
    assert again.status_code == 409

    verified = client.post("/api/spd/redeems/verify",
                           json={"verify_code": record["verify_code"]}, headers=h)
    assert verified.status_code == 200, verified.text
    # 再核销一次：接口按"待核销记录"查，已核销的自然查不到 → 404。
    # 语义没问题（核销码用过就作废），钉住它是防止哪天变成 200 让同一张码兑两次
    assert client.post("/api/spd/redeems/verify",
                       json={"verify_code": record["verify_code"]},
                       headers=h).status_code == 404, "核销码不能重复使用"


def test_积分不够不许兑换(client, h):
    goods = client.post(
        "/api/spd/goods",
        json={"code": "gd_expensive", "name": "血压计", "points": 99999, "stock": 5},
        headers=h,
    ).json()
    resp = client.post("/api/spd/redeems", json={"goods_id": goods["id"]}, headers=h)
    assert resp.status_code == 409 and "积分" in resp.json()["detail"]


def test_签到一天只能签一次(client, h):
    first = client.post("/api/spd/point-accounts/signin", headers=h)
    assert first.status_code in (200, 201, 404), first.text
    if first.status_code == 404:
        pytest.skip("环境未配置签到积分规则")
    again = client.post("/api/spd/point-accounts/signin", headers=h)
    assert again.status_code == 409, "重复签到要 409，不能静默再给一次分"
