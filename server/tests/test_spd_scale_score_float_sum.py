"""量表得分先用浮点逐项累加、再直接和分段上下限比：压线的分被挤进分段缺口，记「未分级」、筛查判「未见异常」、不进目标池
（P2-985，第二十八批「取整与精度发生在哪一步」扫描 F2-1）。

`spd.rules.score_scale` 原先 `total += 分值` 逐项加，再拿这个原值和上下限（含端点，P2-887）比，提示语与返回的得分却是
`round(total, 2)`：0.1+0.2 = 0.30000000000000004 落不进「0–0.3 低危」，0.7+0.1+0.1+0.1 = 0.9999999999999999 落不进「1 起
高危」——风险等级为空、提示语写「得分 1.0 没有落在量表的任何评分分段里」，筛查结论 normal、不进目标池；同一张表把分值 ×10
写成整数，同样作答就判高危、疑似。筛查登记、居民自查、评估三个入口都走这个函数。同类比较的兄弟路径都先按固定精度取整再比
（规则引擎 P2-892、成本分摊）。

修法：逐项分值用 `math.fsum` 相加、取到 6 位小数再落段；返回的得分与提示照旧取两位，整数量表的输出一个字节不变。
"""
import pytest

from app.spd.rules import score_scale

B = "/api/spd"


def _items(scores):
    return [{"key": f"q{i}", "title": f"条目{i}", "type": "single",
             "options": [{"label": "是", "score": s}, {"label": "否", "score": 0}]} for i, s in enumerate(scores)]


def _ranges(low_max, high_min):
    return {"ranges": [{"min": 0, "max": low_max, "risk": "low", "advice": "低危"},
                       {"min": high_min, "max": None, "risk": "high", "advice": "高危，建议复核"}]}


def _all_yes(n):
    return {f"q{i}": "是" for i in range(n)}


def test_压线的小数分落进本该落的那一段():
    low = score_scale(_items([0.1, 0.2]), _all_yes(2), _ranges(0.3, 0.4))
    assert (low["score"], low["risk_level"]) == (0.3, "low"), low   # 修前 risk_level ''、提示「未能按量表分级」
    high = score_scale(_items([0.7, 0.1, 0.1, 0.1]), _all_yes(4), _ranges(0.9, 1.0))
    assert (high["score"], high["risk_level"]) == (1.0, "high"), high   # 修前 0.9999999999999999 落进缺口


def test_数值题按单位折算同样按定精度落段():
    items = [{"key": "cups", "title": "每日饮酒杯数", "type": "number", "score_per_unit": 0.1}]
    got = score_scale(items, {"cups": 3}, _ranges(0.3, 0.4))
    assert (got["score"], got["risk_level"]) == (0.3, "low"), got   # 0.1 × 3 = 0.30000000000000004


def test_整数量表输出不变():
    assert score_scale(_items([7, 1, 1, 1]), _all_yes(4), _ranges(9, 10)) == {
        "score": 10.0, "risk_level": "high", "advice": "高危，建议复核", "answered": 4, "total_items": 4}
    gap = score_scale(_items([7, 1, 1]), _all_yes(3), _ranges(8, 10))   # 真落在缺口里的照旧「未分级」（P2-689）
    assert (gap["score"], gap["risk_level"]) == (9.0, "") and "没有落在量表的任何评分分段里" in gap["advice"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2985 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "P2985_DEC", "name": "P2985 小数分值筛查问卷", "category": "screen", "program_code": "hypertension",
        "version": "v1", "items": _items([0.7, 0.1, 0.1, 0.1]), "scoring": _ranges(0.9, 1.0)})
    assert scale.status_code == 201, scale.text
    assert client.post(f"{B}/scales/{scale.json()['id']}/publish", headers=admin).status_code == 200
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2985 居民", "id_card": "330106196808080085", "birth_date": "1968-08-08"})
    assert patient.status_code in (200, 201), patient.text
    return {"org": org, "patient": patient.json()["id"]}


def test_筛查登记_压线的高分判疑似进目标池(client, admin, world):
    resp = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "org_id": world["org"],
        "scale_code": "P2985_DEC", "answers": _all_yes(4)})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["score"], body["risk_level"], body["result"]) == (1.0, "high", "suspect"), body   # 修前 '' / normal
    pool = client.get(f"{B}/candidates", headers=admin, params={"limit": 100}).json()
    assert world["patient"] in {c["patient_id"] for c in pool}
