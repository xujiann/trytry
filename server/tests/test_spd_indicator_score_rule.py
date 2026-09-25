"""考核指标的评分规则写坏了，整张考核方案一计分就 500（P2-79）。

`score_rule` 把指标值折成得分（`assess.score_of`：ratio 按比例 / step 分档）。建 / 改指标原先照单全收：满分写成
文字、分档不是列表、分档边界或分值不是数，建指标照样 201，`/scores/run` 求到这个指标时抛错——整张方案、所有
考核对象一起 500。界面不编辑评分规则，写得进去的是接口调用方。

修法：建 / 改指标查评分规则的结构（422）；存量里的坏规则计分时逐指标记错、不 500（与公式求值失败同一个处理），
同一方案的其余指标照常计分。存量里的未知类型照旧按「未配置」计分（修前就是这么算的）。
"""
import pytest

B = "/api/spd"
BAD_RULES = [
    ({"type": "ratio", "full": "满分"}, "按比例计分的满分（full）必须是数"),
    ({"type": "ratio", "full": None}, "按比例计分的满分（full）必须是数"),
    ({"type": "ratio", "target": [80]}, "按比例计分的目标值（target）必须是数"),
    ({"type": "step", "steps": "0-60:0"}, "分档计分的 steps 必须是分档列表"),
    ({"type": "step", "steps": [{"min": "六十", "score": 5}]}, "分档的上下限（min / max）必须是数，不设限留空"),
    ({"type": "step", "steps": [{"min": 0, "score": "五"}]}, "分档的分值（score）必须是数"),
]
IDS = ["满分是文字", "满分是null", "目标值是列表", "分档不是列表", "分档边界是文字", "分值是文字"]


def _indicator(client, admin, code, rule):
    return client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": f"{code} 指标", "data_source": "enrollment", "object_type": "org",
        "formula": "enrolled", "score_rule": rule})


@pytest.mark.parametrize(("rule", "detail"), BAD_RULES, ids=IDS)
def test_建指标_计分会抛错的评分规则一律422(client, admin, rule, detail):
    """修前这几种写法建指标全部 201，计分时整张方案 500。"""
    resp = _indicator(client, admin, "P279_BAD", rule)
    assert resp.status_code == 422 and resp.json() == {"detail": f"评分规则非法：{detail}"}, resp.text[:300]


@pytest.mark.parametrize("rule", [{"type": "ranking"}, {"full": 100, "target": 80}], ids=["未知类型", "漏写类型"])
def test_建指标_未知或漏写的类型422(client, admin, rule):
    """修前照收，计分时悄悄按「未配置评分规则」拿指标值当得分。"""
    resp = _indicator(client, admin, "P279_KIND", rule)
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json()["detail"].startswith("评分规则非法：评分规则类型只能是 ratio（按比例）/ step（分档）")


@pytest.mark.parametrize(("code", "rule"), [
    ("P279_OK1", {}), ("P279_OK2", {"type": "ratio"}), ("P279_OK3", {"type": "ratio", "full": 100, "target": None}),
    ("P279_OK4", {"type": "ratio", "full": 100, "target": 0}), ("P279_OK5", {"type": "step", "steps": []}),
    ("P279_OK6", {"type": "step", "steps": [{"min": 1, "score": 100}, {"max": 0}]}),
], ids=["不配", "按比例全缺省", "目标值留空", "目标值为0", "空分档", "开区间分档"])
def test_合法的评分规则照收(client, admin, code, rule):
    resp = _indicator(client, admin, code, rule)
    assert resp.status_code == 201, resp.text[:300]


def test_改指标也查(client, admin):
    created = _indicator(client, admin, "P279_EDIT", {"type": "ratio", "full": 100, "target": 1})
    assert created.status_code == 201, created.text
    url = f"{B}/indicators/{created.json()['id']}"
    resp = client.patch(url, headers=admin, json={"score_rule": {"type": "step", "steps": {"min": 0}}})
    assert resp.status_code == 422 and resp.json() == {"detail": "评分规则非法：分档计分的 steps 必须是分档列表"}
    resp = client.patch(url, headers=admin, json={"name": "P279 改名"})   # 不动评分规则的改档不查
    assert resp.status_code == 200, resp.text


def test_存量坏规则_计分逐指标记错不500_同方案其余指标照常(client, admin):
    """修前落库的坏规则：修前整张方案 500；现在只有这个指标记错、不计分，未知类型照旧按指标值计分。"""
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P279 考核院", "org_type": "township", "level": "township"}).json()["id"]
    good = _indicator(client, admin, "P279_GOOD", {"type": "ratio", "full": 100, "target": 1})
    broken = _indicator(client, admin, "P279_BROKEN", {"type": "ratio", "full": 100, "target": 1})
    legacy_kind = _indicator(client, admin, "P279_LEGACY_KIND", {})
    assert {good.status_code, broken.status_code, legacy_kind.status_code} == {201}
    with SessionLocal() as db:
        db.get(SpdIndicator, broken.json()["id"]).score_rule = {"type": "step", "steps": "0-60:0"}
        db.get(SpdIndicator, legacy_kind.json()["id"]).score_rule = {"type": "ranking"}
        db.commit()
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P279_PLAN", "name": "P279 考核", "level": "township", "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": code, "weight": 30}
                  for code in ("P279_GOOD", "P279_BROKEN", "P279_LEGACY_KIND")]})
    assert plan.status_code == 201, plan.text
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan.json()["id"], "period": "2026-09", "object_ids": [org]})
    assert run.status_code == 200, run.text[:300]
    score_id = client.get(f"{B}/scores?plan_id={plan.json()['id']}", headers=admin).json()[0]["id"]
    detail = {d["indicator_code"]: d for d in client.get(f"{B}/scores/{score_id}", headers=admin).json()["detail"]}
    assert detail["P279_BROKEN"] == {"indicator_code": "P279_BROKEN",
                                     "error": "评分规则非法：分档计分的 steps 必须是分档列表"}
    assert "error" not in detail["P279_GOOD"]
    assert detail["P279_LEGACY_KIND"]["reason"] == "未配置评分规则，按指标值计分"
