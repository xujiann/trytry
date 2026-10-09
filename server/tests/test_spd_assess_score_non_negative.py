"""考核得分不再是负数：负的指标值按 0 计，满分与分档分值写成负数一律 422（P2-1579，第四十六批扫描 AJ4-3）。

按比例计分原先只查满分 `full` 是不是数、不查正负，分档的 `score` 也只查是不是数；`got = round(full * value / target, 2)`
没有下限（同函数「未配置评分规则」那一支却截到 0～100）。扫描实测（修前代码）：公式 `(done - (total - done)) / total * 100`
（页面的公式框就写得出）加按比例计分、目标 90，乙卫生院 1 条任务未办，指标值 -100，原始分 -111.11、加权 -55.55、扣分
105.55，超过它的权重 50，总分 -55.55，得分分析的平均分 -13.88；满分 full:-100 照样 201（完成率 50% 得 -27.78、完成率 0
得 0，达标反而拿最低分），分档 score:-50 也照收。

修法：建 / 改指标时满分与分档分值须不小于 0（422 点名）；按比例计分的得分截在 [0, 满分]，指标值为负时按 0 计、理由写明；
存量里已这样存着的坏规则，计分时与 P2-79 同一处理——逐指标记错、不计分，同方案其余指标照常。负权重（P2-108）、非正目标
（P2-718）早已挡掉，基金分配算出负权重按 0 计（P2-191），这里是同一个口径。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.spd.models import SpdIndicator, SpdTask
from app.spd.routers.assess import score_rule_problem

B = "/api/spd"
#: 任务时间钉在这个月里，与跑分当天是几号无关
PERIOD = "2026-08"
RATIO = {"type": "ratio", "full": 100}
WHY = "负分按权重折算进总分就是倒扣分，扣分会超过这一项的权重"
NEG_FULL = {"type": "ratio", "full": -100}
NEG_STEP = {"type": "step", "steps": [{"min": 0, "max": 49, "score": -50}, {"min": 50, "max": 100, "score": 100}]}
NEG_FULL_DETAIL = f"按比例计分的满分（full）不能小于 0（收到 -100）：{WHY}"
NEG_STEP_DETAIL = f"分档的分值（score）不能小于 0（第 1 档收到 -50）：{WHY}"


@pytest.fixture(scope="module")
def world(client, admin):
    """甲、乙两家卫生院：甲本期 2 条任务办结 1 条，乙 1 条未办（与扫描同一组数）。"""
    orgs = []
    for name in ("甲", "乙"):
        made = client.post("/api/organizations", headers=admin, json={
            "name": f"P21579 {name}卫生院", "org_type": "township", "level": "township"})
        assert made.status_code in (200, 201), made.text
        orgs.append(made.json()["id"])
    patient = client.post("/api/patients", headers=admin, json={"name": "P21579 患者", "id_card": "330106196501011579"})
    assert patient.status_code in (200, 201), patient.text
    with SessionLocal() as db:
        db.add_all([
            SpdTask(patient_id=patient.json()["id"], title="P21579 随访", task_type="followup", org_id=org,
                    status=status, created_at=datetime(2026, 8, 15, 8, 0))
            for org, status in ((orgs[0], "done"), (orgs[0], "pending"), (orgs[1], "pending"))
        ])
        db.commit()
    return {"jia": orgs[0], "yi": orgs[1]}


def _indicator(client, admin, code, rule, formula="done / total * 100"):
    return client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": "task", "object_type": "org",
        "formula": formula, "score_rule": rule, "target_value": 90})


def _run(client, admin, world, code, indicator_codes):
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": code, "name": f"{code} 方案", "level": "township", "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": c, "weight": 50} for c in indicator_codes]})
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan_id, "period": PERIOD, "object_ids": [world["jia"], world["yi"]]})
    assert run.status_code == 200, run.text[:300]
    rows = client.get(f"{B}/scores", headers=admin, params={"plan_id": plan_id, "period": PERIOD}).json()
    out = {}
    for row in rows:
        detail = client.get(f"{B}/scores/{row['id']}", headers=admin).json()["detail"]
        out[row["object_id"]] = (row["total_score"], {d["indicator_code"]: d for d in detail})
    return plan_id, out


def test_判据_满分与分档分值不得为负_点名():
    assert score_rule_problem(NEG_FULL) == NEG_FULL_DETAIL
    assert score_rule_problem(NEG_STEP) == NEG_STEP_DETAIL
    assert score_rule_problem({"type": "step", "steps": [{"max": 59, "score": 0}, {"min": 60, "score": -0.5}]}) == (
        f"分档的分值（score）不能小于 0（第 2 档收到 -0.5）：{WHY}")
    # 0 照收：满分 0 的指标只作展示、不计分；低档给 0 分是常见写法
    assert score_rule_problem({"type": "ratio", "full": 0}) == ""
    assert score_rule_problem({"type": "step", "steps": [{"max": 59, "score": 0}, {"min": 60, "score": 100}]}) == ""


@pytest.mark.parametrize(("rule", "detail"), [(NEG_FULL, NEG_FULL_DETAIL), (NEG_STEP, NEG_STEP_DETAIL)],
                         ids=["满分-100", "分档-50"])
def test_建指标_负满分与负分档_422(client, admin, rule, detail):
    resp = _indicator(client, admin, "P21579_BAD", rule)
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": f"评分规则非法：{detail}"}


def test_改指标_改成负满分或负分档_422_原值不变(client, admin):
    created = _indicator(client, admin, "P21579_EDIT", RATIO)
    assert created.status_code == 201, created.text
    url = f"{B}/indicators/{created.json()['id']}"
    for rule, detail in ((NEG_FULL, NEG_FULL_DETAIL), (NEG_STEP, NEG_STEP_DETAIL)):
        resp = client.patch(url, headers=admin, json={"score_rule": rule})
        assert resp.status_code == 422, resp.text   # 修前 200
        assert resp.json() == {"detail": f"评分规则非法：{detail}"}
    with SessionLocal() as db:
        assert db.get(SpdIndicator, created.json()["id"]).score_rule == RATIO


def test_负的指标值得0分_扣分不超过权重_正常规则分数不变(client, admin, world):
    net = _indicator(client, admin, "P21579_NET", RATIO, formula="(done - (total - done)) / total * 100")
    rate = _indicator(client, admin, "P21579_RATE", RATIO)
    assert (net.status_code, rate.status_code) == (201, 201), (net.text, rate.text)
    plan_id, scores = _run(client, admin, world, "P21579_PLAN", ["P21579_NET", "P21579_RATE"])

    total, detail = scores[world["yi"]]
    item = detail["P21579_NET"]
    # 修前：原始分 -111.11、加权 -55.55、扣分 105.55（超过权重 50），总分 -55.55
    assert (item["value"], item["raw_score"], item["score"], item["deduction"]) == (-100.0, 0.0, 0.0, 50.0), item
    assert item["reason"] == "未达目标值90.0（实际-100.0），指标值为负，按 0 计"
    assert total == 0.0

    # 正常规则分数不变：甲净完成率 0 得 0 分；完成率 50% 原始分 55.56、加权 27.78，与修前同一个数
    total, detail = scores[world["jia"]]
    assert (detail["P21579_NET"]["raw_score"], detail["P21579_NET"]["reason"]) == (0.0, "未达目标值90.0（实际0.0）")
    rate_item = detail["P21579_RATE"]
    assert (rate_item["raw_score"], rate_item["score"], rate_item["reason"]) == (55.56, 27.78, "未达目标值90.0（实际50.0）")
    assert total == 27.78

    for _, items in scores.values():
        for d in items.values():
            assert d["score"] >= 0 and d["deduction"] <= d["weight"], d
    analysis = client.get(f"{B}/scores-analysis", headers=admin, params={"plan_id": plan_id, "period": PERIOD}).json()
    assert analysis["average"] == 13.89   # 修前 -13.88


def test_存量里的负满分与负分档_计分逐指标记错_同方案其余照常(client, admin, world):
    """写入口查它之前存下的：与 P2-79 的坏评分规则同一处理，不计分、明细里写明，同方案其余指标照常出分。"""
    codes = ("P21579_LEGACY_FULL", "P21579_LEGACY_STEP", "P21579_LEGACY_OK")
    for code in codes:
        made = _indicator(client, admin, code, RATIO)
        assert made.status_code == 201, made.text
    with SessionLocal() as db:
        stored = {i.code: i for i in db.query(SpdIndicator).filter(SpdIndicator.code.in_(codes))}
        stored["P21579_LEGACY_FULL"].score_rule = NEG_FULL
        stored["P21579_LEGACY_STEP"].score_rule = NEG_STEP
        db.commit()
    _, scores = _run(client, admin, world, "P21579_LEGACY_PLAN", list(codes))
    total, detail = scores[world["jia"]]
    # 修前：负满分照算出 -27.78（完成率 50%），负分档落在 50～100 那档照给分，都不报错
    assert detail["P21579_LEGACY_FULL"] == {"indicator_code": "P21579_LEGACY_FULL",
                                            "error": f"评分规则非法：{NEG_FULL_DETAIL}"}
    assert detail["P21579_LEGACY_STEP"] == {"indicator_code": "P21579_LEGACY_STEP",
                                            "error": f"评分规则非法：{NEG_STEP_DETAIL}"}
    assert "error" not in detail["P21579_LEGACY_OK"]
    assert total == detail["P21579_LEGACY_OK"]["score"] == 27.78
