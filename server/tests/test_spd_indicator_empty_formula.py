"""考核指标公式留空照收，纳管 / 评估 / 建档 / 上报四个口径恒 0 分；同口径写 `total` 反而 422；能算的公式被严格试算挡在
门外（P2-1119，第三十二批「规则与配置的求值口径」扫描 B4-5）。

`create_indicator` 只在 `if body.formula:` 时校验，`update_indicator` 用 `if changes.get("formula")`，PATCH `formula: ""`
直接清空、不校验；计分（`run_scoring`）与报告段落（`reporting._indicator`）都是「公式留空按 `total` 取值」，而
`service.INDICATOR_SOURCES` 里这四个口径根本没有 total——`metrics.get("total", 0)` 恒 0，理由写「未达目标值」、不记错，
看着像真没做到。页面把公式框设成必填，只挡住了页面。修前实测（b4/r2）：纳管口径写 `total` 422「未知变量：total」，写 `''`
201；同一机构在管 2 人、目标 2，空公式 `total_score = 0.0`、`error = None`，写 `enrolled` 得 100。

另一半（b4/r2b）：建 / 改指标拿严格的 `evaluate` 代哑值 1 试算，`(total - done - overdue) ** 0.5` 在三者都是 1 时开负数的
平方根，422「负数不能开非整数次方」；同形状的平台绩效公式 201，真实计数下算得出 1.7321。`formula.validate` 的说明写着
「哑值下算不出不算表达式写错」，平台绩效公式与基金分配公式都用它。

修法：建 / 改指标改用 `formula.validate`（与平台绩效同一口径），公式留空按 `total` 查——与写 `total` 同一句 422；PATCH
清空公式同样查；计分与报告段落把留空的公式当 `total` 求值，存量里口径没有 total 的空公式逐指标记错、不给 0 分。
"""
from types import SimpleNamespace

import pytest

from app.spd import reporting
from app.spd.routers import assess

B = "/api/spd"
NO_TOTAL = ["enrollment", "assessment", "archive", "case_report"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1119 考核院", "org_type": "township", "level": "township"}).json()["id"]
    for i in range(2):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P1119 患者{i}", "id_card": f"33012719680202111{i}"}).json()["id"]
        enrollment = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": org})
        assert enrollment.status_code == 201, enrollment.text
    return {"org": org}


def _indicator(client, admin, code, source, formula):
    return client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": source, "object_type": "org", "formula": formula,
        "target_value": 2, "score_rule": {"type": "ratio", "full": 100}})


@pytest.mark.parametrize("source", NO_TOTAL)
def test_口径没有total_公式留空与写total同一句422(client, admin, source):
    """修前留空 201，计分恒 0 分、不记错。"""
    written = _indicator(client, admin, f"P1119_T_{source}", source, "total")
    empty = _indicator(client, admin, f"P1119_E_{source}", source, "")
    assert written.status_code == empty.status_code == 422, empty.text[:300]
    assert written.json() == empty.json() == {"detail": "公式非法：未知变量：total"}


def test_口径有total_公式留空照收(client, admin):
    assert _indicator(client, admin, "P1119_TASK_EMPTY", "task", "").status_code == 201


def test_改指标清空公式同样422_公式不变(client, admin):
    created = _indicator(client, admin, "P1119_PATCH", "enrollment", "enrolled")
    assert created.status_code == 201, created.text
    patched = client.patch(f"{B}/indicators/{created.json()['id']}", headers=admin, json={"formula": ""})
    assert patched.status_code == 422, patched.text[:300]   # 修前 200，公式被清空
    assert patched.json() == {"detail": "公式非法：未知变量：total"}
    row = next(i for i in client.get(f"{B}/indicators", headers=admin, params={"limit": 500}).json()
               if i["id"] == created.json()["id"])
    assert row["formula"] == "enrolled"


def test_哑值下算不出_真实取值算得出的公式照收(client, admin):
    """与平台绩效公式同一个校验口径（formula.validate）：修前 422「负数不能开非整数次方」。"""
    formula = "(total - done - overdue) ** 0.5"
    created = _indicator(client, admin, "P1119_SQRT", "task", formula)
    assert created.status_code == 201, created.text[:300]
    other = _indicator(client, admin, "P1119_SQRT2", "task", "done")
    patched = client.patch(f"{B}/indicators/{other.json()['id']}", headers=admin, json={"formula": formula})
    assert patched.status_code == 200 and patched.json()["formula"] == formula, patched.text[:300]
    # 写错的照样拦：语法、未知变量
    assert _indicator(client, admin, "P1119_BAD1", "task", "done +").status_code == 422
    assert _indicator(client, admin, "P1119_BAD2", "enrollment", "enrolled / total").json() == {
        "detail": "公式非法：未知变量：total"}


def test_存量空公式_口径没有total_跑分记错不给0分(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator

    with SessionLocal() as db:   # 修前经接口建进去的空公式指标
        db.add(SpdIndicator(code="P1119_LEGACY", name="P1119 存量空公式", object_type="org", data_source="enrollment",
                            formula="", target_value=2, score_rule={"type": "ratio", "full": 100}))
        db.commit()
    assert _indicator(client, admin, "P1119_OK", "enrollment", "enrolled").status_code == 201
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P1119_PLAN", "name": "P1119 考核", "level": "township", "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": "P1119_LEGACY", "weight": 50}, {"indicator_code": "P1119_OK", "weight": 50}]})
    assert plan.status_code == 201, plan.text
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan.json()["id"], "period": "2099-12", "object_ids": [world["org"]]})
    assert run.status_code == 200, run.text[:300]
    score_id = client.get(f"{B}/scores?plan_id={plan.json()['id']}", headers=admin).json()[0]["id"]
    legacy, ok = client.get(f"{B}/scores/{score_id}", headers=admin).json()["detail"]
    # 修前 value 0.0、score 0.0、reason「未达目标值2.0（实际0.0）」，没有 error
    assert legacy == {"indicator_code": "P1119_LEGACY", "error": "公式求值失败：未知变量：total"}
    assert (ok["value"], ok["raw_score"]) == (2.0, 100.0), ok


def _report(monkeypatch, source_metrics):
    indicator = SimpleNamespace(code="P1119R", name="P1119 纳管数", object_type="org", formula="", target_value=None,
                                version="v1")
    monkeypatch.setattr(assess, "effective_versions", lambda db, codes, period: ({"P1119R": indicator}, set()))
    monkeypatch.setattr(assess, "collect_metrics", lambda db, ind, kind, org_id, period: source_metrics)
    return reporting._indicator(None, {"key": "indicator", "indicator_code": "P1119R", "period": "2026-09"}, 1, "monthly")


def test_报告段落与计分同源_空公式口径没有total写明求值失败(monkeypatch):
    got = _report(monkeypatch, {"enrolled": 2.0, "target": 2.0, "high_risk": 0.0})
    assert "value" not in got and got["note"] == "公式求值失败：未知变量：total", got   # 修前印「P1119 纳管数：0.0」


def test_报告段落_口径有total的空公式照旧按total取值(monkeypatch):
    assert _report(monkeypatch, {"total": 5.0, "done": 3.0, "overdue": 1.0})["text"] == "P1119 纳管数：5.0"
