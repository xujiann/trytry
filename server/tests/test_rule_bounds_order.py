"""规则里的区间与比较值在配置时查顺序与类型：上下界颠倒、界不是数、比较值不是数一律 422（P2-712，第十八批「数值入参
的符号与业务上下界」扫描 V3-3）。

求值时读不成数就判不命中、区间按 `low <= 值 <= high` 比（`spd.rules._match_one`）：「介于 200,160」「180,」（页面把空段
读成 0）「180，abc」（读成 null）、「≥ 18O」（字母 O）都能建（201），之后永远不命中、也不报错——问卷异常规则漏派「立即
上转评估」，转诊规则试算不命中。数据质控的区间 min 300 / max 50 把每一行都判成违规。同形的量表分段、考核分档同样只查
是不是数、不查先后。规矩早就立过：区间要查起止顺序（P2-56），NaN 一类「永不命中、也不报错」的比较值在配置时拦（P2-466），
兄弟路径的慢专病目标「下限不得大于上限」、冷链区间「上限须大于下限」都在拦。

修法：`validate_conditions`（问卷异常规则 / 转诊规则 / 纳入排除与路径节点条件 / 分组规则共用）里，大于小于类的比较值
必须读得成有限的数，「介于」两界都必须是数且下限不大于上限；数据质控区间、量表评分分段、考核分档同样查下限不大于上限。
存进去的旧规则按规则批量入组时照 P1-123 报 422、说清楚。
"""
import pytest

B = "/api/spd"


@pytest.mark.parametrize("bounds", [[200, 160], [180, None], [180, "abc"]])
def test_问卷异常规则_介于上下界颠倒或不是数_422(client, admin, bounds):
    resp = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": f"p2712_q{bounds[1]}", "name": "P2712 问卷", "scene": "outpatient",
        "items": [{"key": "bp_sys", "title": "自测收缩压", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "bp_sys", "op": "between", "value": bounds},
                            "level": "high", "action": "立即上转评估"}]})
    assert resp.status_code == 422, resp.text   # 修前 201，作答 185 判「无异常」


def test_问卷异常规则_介于写对了照收(client, admin):
    resp = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "p2712_ok", "name": "P2712 问卷", "scene": "outpatient",
        "items": [{"key": "bp_sys", "title": "自测收缩压", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "bp_sys", "op": "between", "value": [160, 250]},
                            "level": "high", "action": "立即上转评估"}]})
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("cond", [
    {"field": "bp_sys", "op": ">=", "value": "18O"},
    {"field": "bp_sys", "op": "between", "value": [250, 180]},
])
def test_转诊规则_比较值不是数或区间颠倒_422(client, admin, cond):
    resp = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": f"p2712_rr_{cond['op']}", "name": "P2712 转诊规则", "conditions": [cond]})
    assert resp.status_code == 422, resp.text   # 修前 201，收缩压 185 试算不命中


def test_转诊规则_读得成数的文本照收(client, admin):
    resp = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "p2712_rr_ok", "name": "P2712 转诊规则", "conditions": [{"field": "bp_sys", "op": ">=", "value": "180"}]})
    assert resp.status_code == 201, resp.text


def test_数据质控区间下限大于上限_422(client, admin):
    from app.models import FollowUp

    def rule(code, cfg):
        return client.post("/api/dataquality/rules", headers=admin, json={
            "code": code, "name": f"收缩压区间{code}", "target_table": FollowUp.__tablename__, "rule_type": "range",
            "config": cfg})

    assert rule("P2712_REV", {"field": "sbp", "min": 300, "max": 50}).status_code == 422   # 修前 201，每一行都判违规
    assert rule("P2712_OK", {"field": "sbp", "min": 50, "max": 300}).status_code in (200, 201)


def test_量表评分分段下限大于上限_422(client, admin):
    resp = client.post(f"{B}/scales", headers=admin, json={
        "code": "p2712_scale", "name": "P2712 量表",
        "items": [{"key": "q1", "title": "题一", "type": "single", "options": [{"label": "是", "score": 5}]}],
        "scoring": {"ranges": [{"min": 10, "max": 5, "risk": "high", "advice": "转诊"}]}})
    assert resp.status_code == 422, resp.text   # 修前 201，这一段永远落不进去


def test_考核分档下限大于上限_422(client, admin):
    resp = client.post(f"{B}/indicators", headers=admin, json={
        "code": "p2712_ind", "name": "P2712 指标", "data_source": "task", "formula": "done / total * 100",
        "score_rule": {"type": "step", "steps": [{"min": 90, "max": 60, "score": 10}]}})
    assert resp.status_code == 422, resp.text   # 修前 201，这一档永远命中不了
