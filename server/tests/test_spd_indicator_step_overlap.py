"""考核指标分档计分不查档与档的重叠：压线值落哪档看书写顺序（P2-1120，第三十二批「规则与配置的求值口径」扫描 B4-7 的
重叠一半）。

`score_rule_problem` 只查单档下限不大于上限（P2-712），档与档重叠照收；`score_of` 上下限都含、取第一个命中的档。修前实测
（b4/r6）：两档 [60–80, 80–100]，指标值 80 得 60 分；同样两档倒过来写得 100 分——分数取决于书写顺序。同子系统的量表
评分分段早已按 P2-887 查重叠（`rules.scale_overlap_problem`）。

修法：建 / 改指标时复用 `scale_overlap_problem`（与量表分段同一个口径，上下限都含），重叠 422；只查写入口、不进
`score_rule_problem`（那一句计分时也查），存量的照旧按书写顺序计分。档与档之间的缺口（0–59 / 60–79 / 80–100 下
79.5 落空给 0 分）要定半开区间还是首尾相接，待裁定，不在此列——首尾相接的写法照收。
"""
import pytest

B = "/api/spd"
OVERLAP = [{"min": 60, "max": 80, "score": 60}, {"min": 80, "max": 100, "score": 100}]
ADJACENT = [{"min": 0, "max": 59, "score": 0}, {"min": 60, "max": 79, "score": 60}, {"min": 80, "max": 100, "score": 100}]


def _indicator(client, admin, code, steps, formula="done"):
    return client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": "task", "object_type": "org", "formula": formula,
        "score_rule": {"type": "step", "steps": steps}})


@pytest.mark.parametrize(("steps", "pair"), [
    (OVERLAP, "「60–80」与「80–100」"),
    (list(reversed(OVERLAP)), "「80–100」与「60–80」"),
    ([{"min": 0, "max": 70, "score": 60}, {"min": 60, "max": 100, "score": 100}], "「0–70」与「60–100」"),
    ([{"min": 80, "score": 100}, {"min": 90, "score": 60}], "「80 起」与「90 起」"),
], ids=["压线重叠", "倒过来写", "区间交叠", "两档都不封顶"])
def test_建指标_分档重叠422(client, admin, steps, pair):
    """修前四种写法都 201：压线的指标值落进先写的那一档。"""
    resp = _indicator(client, admin, "P1120_BAD", steps)
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json() == {"detail": f"评分规则非法：评分分段{pair}有重叠（上下限都含）：压线的得分落进哪一段取决于书写顺序，"
                                     "请把相邻两段错开（如 0–3、4–6）"}


def test_首尾相接的分档照收_改指标同一句(client, admin):
    created = _indicator(client, admin, "P1120_OK", ADJACENT)
    assert created.status_code == 201, created.text[:300]
    iid = created.json()["id"]
    patched = client.patch(f"{B}/indicators/{iid}", headers=admin, json={"score_rule": {"type": "step", "steps": OVERLAP}})
    assert patched.status_code == 422, patched.text[:300]   # 修前 200
    assert "有重叠" in patched.json()["detail"]
    kept = client.patch(f"{B}/indicators/{iid}", headers=admin, json={"score_rule": {"type": "step", "steps": ADJACENT[1:]}})
    assert kept.status_code == 200, kept.text[:300]


def test_存量重叠分档照旧按书写顺序计分_不记错(client, admin):
    """只拦写入口：计分时查的 `score_rule_problem` 不含重叠，存量方案照常出分（与量表分段 P2-887 同一个取舍）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1120 考核院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        db.add(SpdIndicator(code="P1120_LEGACY", name="P1120 存量重叠分档", object_type="org", data_source="task",
                            formula="80", score_rule={"type": "step", "steps": OVERLAP}))
        db.commit()
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P1120_PLAN", "name": "P1120 考核", "level": "township", "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": "P1120_LEGACY", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan.json()["id"], "period": "2099-12", "object_ids": [org]})
    assert run.status_code == 200, run.text[:300]
    score_id = client.get(f"{B}/scores?plan_id={plan.json()['id']}", headers=admin).json()[0]["id"]
    (item,) = client.get(f"{B}/scores/{score_id}", headers=admin).json()["detail"]
    assert "error" not in item and item["raw_score"] == 60.0, item
