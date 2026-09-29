"""按比例计分的考核指标，目标值须大于 0（P2-718，第十八批「数值入参的符号与业务上下界」扫描 V3-7）。

`score_of` 的按比例计分：达标（实际 >= 目标）满分，未达标按 `满分 × 实际 / 目标` 折算。建 / 改指标的目标值
（`target_value: FiniteFloat | None`）与评分规则里的 `target` 只查「是数」：目标 -85 时 `实际 >= 目标` 恒成立，
本期一个都没纳管也满分；目标 0 被 `float(preset or 100)` 悄悄换成 100，扣分理由写「未达目标值100.0」。

修法：按比例计分的指标，目标值与规则里的 target 都须大于 0（建 / 改 422）；计分时存量里的非正目标逐指标记错、
不计分，与坏评分规则同一个处理（P2-79）。分档计分的目标值只作展示，「目标 0 例」照收。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdIndicator

B = "/api/spd"
RATIO = {"type": "ratio", "full": 100}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2718 考核院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org}


def _indicator(client, admin, code, target_value, rule):
    return client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": "enrollment", "object_type": "org",
        "formula": "enrolled", "target_value": target_value, "score_rule": rule})


@pytest.mark.parametrize(("suffix", "target_value", "rule"), [
    ("NEG", -85, RATIO), ("ZERO", 0, RATIO), ("RULE0", None, {**RATIO, "target": 0}),
    ("RULENEG", None, {**RATIO, "target": -5}),
], ids=["目标值负数", "目标值0", "规则target为0", "规则target负数"])
def test_按比例计分的目标不是正数_建指标422(client, admin, suffix, target_value, rule):
    resp = _indicator(client, admin, f"P2718_BAD_{suffix}", target_value, rule)
    assert resp.status_code == 422 and "须大于 0" in resp.json()["detail"], resp.text   # 修前 201


def test_分档计分的目标0照收_按比例的正目标照收(client, admin):
    step = _indicator(client, admin, "P2718_STEP", 0, {"type": "step", "steps": [{"max": 0, "score": 100}]})
    assert step.status_code == 201, step.text
    ratio = _indicator(client, admin, "P2718_OK", 85, RATIO)
    assert ratio.status_code == 201, ratio.text


def test_改指标_改成非正目标或改成按比例而目标非正的422_原值不变(client, admin):
    ratio = _indicator(client, admin, "P2718_EDIT", 85, RATIO).json()["id"]
    resp = client.patch(f"{B}/indicators/{ratio}", headers=admin, json={"target_value": -1})
    assert resp.status_code == 422, resp.text   # 修前 200
    step = _indicator(client, admin, "P2718_EDIT_STEP", 0, {"type": "step", "steps": [{"score": 100}]}).json()["id"]
    resp = client.patch(f"{B}/indicators/{step}", headers=admin, json={"score_rule": RATIO})
    assert resp.status_code == 422, resp.text   # 改成按比例、目标仍是 0
    with SessionLocal() as db:
        assert db.get(SpdIndicator, ratio).target_value == 85
        assert db.get(SpdIndicator, step).score_rule["type"] == "step"
    renamed = client.patch(f"{B}/indicators/{ratio}", headers=admin, json={"name": "P2718 改名不受影响"})
    assert renamed.status_code == 200, renamed.text


def test_存量里的负目标_计分逐指标记错_不给满分(client, admin, world):
    ind = _indicator(client, admin, "P2718_LEGACY", 85, RATIO).json()["id"]
    with SessionLocal() as db:   # 写入口查它之前存下的
        db.get(SpdIndicator, ind).target_value = -85
        db.commit()
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P2718_PLAN", "name": "P2718 考核", "level": "township", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": "P2718_LEGACY", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan.json()["id"], "period": "2099-12", "object_ids": [world["org"]]})
    assert run.status_code == 200, run.text[:300]
    score = client.get(f"{B}/scores?plan_id={plan.json()['id']}", headers=admin).json()[0]
    detail = client.get(f"{B}/scores/{score['id']}", headers=admin).json()["detail"][0]
    assert detail.get("error", "").startswith("目标值非法：按比例计分的指标目标值须大于 0（收到 -85）"), detail
    assert score["total_score"] == 0, score   # 修前：本期一个都没纳管也 100 分
