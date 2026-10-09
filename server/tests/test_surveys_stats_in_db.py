"""满意度统计在库里按评价对象、分数分组数（P2-1670，第四十九批扫描 AM4-8）。

`GET /api/surveys/stats`（满意度页每次渲染都调）原先 `q.all()` 把满意度表整表载入 ORM、在内存里分组：扫描实测 1 万行
0.35 s、峰值 13.5 MB，5 万行 3.02 s、67.7 MB，随行数线性增长；居民端每交一次评价就多一行（P2-810），没有上限。同形的随访
中心统计已按 P2-1151 改成库内 GROUP BY。

修后一条 `GROUP BY target_type, score` 取计数，均分、分布、差评数都由计数算出，出参逐字节不变：取整照旧是 Python 的
`round`（P2-1001 记过的两种取整口径不动），键序照 `SurveyStatsOut`，空表照旧 `[]`，按评价对象排序。

- 特征化：空表、单一对象（含 P2-1001 那个 1 / 32 的取整）、随机数据（含接口不收、库里可能有的表外对象）、带筛选参数各一组，
  逐项与原算法相同——改前改后都绿；
- 缺陷：请求里载入的满意度 ORM 对象为 0（修前一条评价一个），评价翻几番取回的行数也不变。
"""
import json
import random

import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import Patient, SatisfactionSurvey
from app.routers.surveys import NEGATIVE_SCORE

#: 出参键序（`SurveyStatsOut` 的声明顺序，序列化照它走）
KEYS = ["target_type", "distribution", "negative", "count", "avg_score", "negative_rate_pct"]


def _原算法(db, target_type: str | None = None) -> list[dict]:
    """修前的算法原样搬来当判据：满意度整表载入，逐条分组。"""
    q = db.query(SatisfactionSurvey)
    if target_type:
        q = q.filter(SatisfactionSurvey.target_type == target_type)
    grouped: dict[str, dict] = {}
    for s in q.all():
        entry = grouped.setdefault(
            s.target_type,
            {"target_type": s.target_type, "count": 0, "total": 0,
             "distribution": {str(i): 0 for i in range(1, 6)}, "negative": 0},
        )
        entry["count"] += 1
        entry["total"] += s.score
        entry["distribution"][str(s.score)] += 1
        if s.score <= NEGATIVE_SCORE:
            entry["negative"] += 1
    result = []
    for entry in grouped.values():
        count = entry.pop("count")
        total = entry.pop("total")
        entry["count"] = count
        entry["avg_score"] = round(total / count, 2) if count else 0.0
        entry["negative_rate_pct"] = round(entry["negative"] * 100 / count, 2) if count else 0.0
        result.append(entry)
    return sorted(result, key=lambda x: x["target_type"])


@pytest.fixture(scope="module")
def patient(client, admin):
    with SessionLocal() as db:
        p = Patient(ehc_no="EHC-P21670-0001", name="满意度统计患者", id_card="330382197007071670")
        db.add(p)
        db.commit()
        return p.id


def _add(patient_id: int, rows: list[tuple[str, int]]) -> None:
    with SessionLocal() as db:
        db.execute(insert(SatisfactionSurvey), [
            {"target_type": kind, "target_id": 1, "patient_id": patient_id, "score": score, "comment": ""}
            for kind, score in rows
        ])
        db.commit()


def _stats(client, admin, target_type: str | None = None) -> tuple[list[dict], int]:
    """（响应, 这一请求里载入的满意度 ORM 对象个数）；顺带钉住每行的键序与两个比率是浮点。"""
    loaded = [0]

    def on_load(target, context):
        loaded[0] += 1

    event.listen(SatisfactionSurvey, "load", on_load)
    try:
        resp = client.get("/api/surveys/stats", headers=admin,
                          params={"target_type": target_type} if target_type else None)
    finally:
        event.remove(SatisfactionSurvey, "load", on_load)
    assert resp.status_code == 200, resp.text
    body = json.loads(resp.content)
    for row in body:
        assert list(row) == KEYS
        assert list(row["distribution"]) == ["1", "2", "3", "4", "5"]
        assert isinstance(row["avg_score"], float) and isinstance(row["negative_rate_pct"], float)
    return body, loaded[0]


def _same_as_before(body: list[dict], target_type: str | None = None) -> None:
    with SessionLocal() as db:
        assert body == _原算法(db, target_type)


def test_空表_回空列表(client, admin, patient):
    body, _ = _stats(client, admin)
    assert body == []
    _same_as_before(body)


def test_单一对象_逐项与原算法相同_取整口径不变(client, admin, patient):
    """P2-1001 那组数：一类评价对象 32 条、1 条差评——差评率 3.125 照旧按 Python round 印 3.12，均分 4.875 印 4.88。"""
    _add(patient, [("encounter", 1)] + [("encounter", 5)] * 31)
    body, _ = _stats(client, admin)
    assert body == [{"target_type": "encounter", "distribution": {"1": 1, "2": 0, "3": 0, "4": 0, "5": 31},
                     "negative": 1, "count": 32, "avg_score": 4.88, "negative_rate_pct": 3.12}]
    _same_as_before(body)


def test_随机数据_逐项与原算法相同(client, admin, patient):
    rng = random.Random(1670)
    kinds = ["contract", "encounter", "consultation", "other"]   # other：接口不收、库里可能有的表外对象
    _add(patient, [(rng.choice(kinds), rng.randint(1, 5)) for _ in range(400)])
    _add(patient, [("contract", 2)] * 3)                          # 差评阈值上那一档
    body, _ = _stats(client, admin)
    assert [r["target_type"] for r in body] == sorted(kinds)
    _same_as_before(body)


@pytest.mark.parametrize("target_type", ["contract", "encounter", "consultation", "other", "no-such-target"])
def test_带筛选参数_逐项与原算法相同(client, admin, patient, target_type):
    body, _ = _stats(client, admin, target_type)
    _same_as_before(body, target_type)
    assert [r["target_type"] for r in body] == ([] if target_type == "no-such-target" else [target_type])


def test_不再把满意度整表载入_ORM(client, admin, patient):
    _, loaded = _stats(client, admin)
    assert loaded == 0, f"统计一次载入了 {loaded} 个满意度 ORM 对象——还在整表读进内存分组"
    _, loaded = _stats(client, admin, "contract")
    assert loaded == 0, loaded


def _rows_fetched(client, admin, url: str) -> int:
    """这一个请求里全部 SELECT 取回的行数：拦下每条语句与参数，请求结束后在同一份数据上重放一遍数行数。"""
    seen: list[tuple] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get(url, headers=admin)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text
    assert seen, "没拦到任何 SELECT（用例失效，请检查拦截方式）"
    with engine.connect() as conn:
        return sum(len(conn.exec_driver_sql(statement, parameters).fetchall()) for statement, parameters in seen)


def test_评价翻几番_取回行数不变(client, admin, patient):
    url = "/api/surveys/stats"
    before = _rows_fetched(client, admin, url)
    # 已有的「对象 × 分数」组合里加量，不加任何一个分组
    _add(patient, [(("contract", "encounter", "consultation", "other")[i % 4], 1 + i % 5) for i in range(200)])
    after = _rows_fetched(client, admin, url)
    assert after == before, f"多了 200 条评价，请求多取回了 {after - before} 行——还在整表读出来分组"
    body, _ = _stats(client, admin)
    _same_as_before(body)
