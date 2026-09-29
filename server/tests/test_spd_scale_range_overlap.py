"""量表评分分段不能重叠（P2-887，第二十四批「阈值与边界值」扫描 Z4-4）。

`score_scale` 上下限都含、取第一个命中的段；`scale_problem` 只查每段下限不大于上限。照「0–3 低危 / 3–6 中危 / 6 起高危」
写，建量表 201、发布 200，压线的 3 分判低危（不进目标池）、6 分判中危；同样三段倒过来写，3 分变中危、6 分变高危——同文件
`grade_abnormal` 的规矩是书写顺序不该决定分级。修后建 / 改 / 发布量表时分段重叠 422，点名是哪两段；四张预置量表照常通过。
已发布的存量量表照常作答（那一句不进作答时的检查，否则整张作答不了），出新版本时改。
"""
from app.database import SessionLocal
from app.spd.models import SpdScale
from app.spd.rules import scale_overlap_problem, score_scale
from app.spd.seed import SEED_SCALES

B = "/api/spd"
ITEMS = [{"key": "q1", "title": "题一", "type": "single", "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]}]
TOUCHING = {"ranges": [{"min": 0, "max": 3, "risk": "low"}, {"min": 3, "max": 6, "risk": "mid"},
                       {"min": 6, "max": None, "risk": "high"}]}


def test_首尾相接的分段_压线分随书写顺序变():
    """修前的形状（纯函数，修后不变）：同样三段、不同行序，3 分的结论不同——这正是建量表时要拦的原因。"""
    reversed_ranges = {"ranges": list(reversed(TOUCHING["ranges"]))}
    assert score_scale(ITEMS, {"q1": "是"}, TOUCHING)["risk_level"] == "low"
    assert score_scale(ITEMS, {"q1": "是"}, reversed_ranges)["risk_level"] == "mid"


def test_建_改_发布量表_分段重叠422(client, admin):
    body = {"code": "P2887_S", "name": "P2887 量表", "version": "1.0", "items": ITEMS, "scoring": TOUCHING}
    got = client.post(f"{B}/scales", headers=admin, json=body)
    assert got.status_code == 422, got.text   # 修前 201
    assert got.json()["detail"] == ("量表配置非法：评分分段「0–3」与「3–6」有重叠（上下限都含）：压线的得分落进哪一段取决于"
                                    "书写顺序，请把相邻两段错开（如 0–3、4–6）")
    ok_scoring = {"ranges": [{"min": 0, "max": 2, "risk": "low"}, {"min": 3, "max": 5, "risk": "mid"},
                             {"min": 6, "risk": "high"}]}
    made = client.post(f"{B}/scales", headers=admin, json={**body, "scoring": ok_scoring})
    assert made.status_code == 201, made.text
    got = client.patch(f"{B}/scales/{made.json()['id']}", headers=admin, json={"scoring": TOUCHING})
    assert got.status_code == 422, got.text   # 改也查
    got = client.patch(f"{B}/scales/{made.json()['id']}", headers=admin, json={"scoring": {"ranges": [
        {"min": None, "max": 3, "risk": "low"}, {"min": 1, "max": None, "risk": "high"}]}})
    assert got.status_code == 422 and "「3 及以下」与「1 起」有重叠" in got.json()["detail"], got.text
    with SessionLocal() as db:   # 修前存下的草稿：发布时拦下
        db.get(SpdScale, made.json()["id"]).scoring = TOUCHING
        db.commit()
    got = client.post(f"{B}/scales/{made.json()['id']}/publish", headers=admin)
    assert got.status_code == 422, got.text   # 修前 200


def test_预置量表都不重叠():
    assert SEED_SCALES
    assert [s["code"] for s in SEED_SCALES if scale_overlap_problem(s["scoring"])] == []
