"""满意度明细给一页配姓名只按本页的患者号取（P2-1153，第三十三批扫描 A4-9）。

`GET /api/surveys`（满意度页一次调两遍：明细与差评清单）原先为了给一页（至多 500 行）配姓名，把整张患者表的（id, 姓名）读进
内存：患者 6 万时返回 50 行、取回 60,052 行、332 ms、19.5 MB（扫描实测）。随访中心的 `followups._name_maps` 早就只按本页的
患者号用 `in_()` 取。

修后照 `_name_maps` 的写法按本页的患者号取，本页为空就不查；响应不变（姓名不是加密列，PII 加密开态下照旧直读）。

- 特征化：两页、差评筛选、空页的每一行与原算法（整表对照姓名）逐项相同——改前改后都绿；
- 缺陷：多建一批没有评价的患者，请求取回的行数不变（修前每个患者一行）。
"""
import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import Patient, SatisfactionSurvey

#: （患者序号, 对象类型, 分数）
SURVEYS = [(0, "encounter", 5), (1, "contract", 2), (0, "consultation", 1), (2, "encounter", 4), (1, "encounter", 3),
           (3, "contract", 5), (2, "consultation", 2)]


@pytest.fixture(scope="module")
def world(client, admin):
    with SessionLocal() as db:
        patients = [Patient(ehc_no=f"EHC-SVNAME-{i}", name=f"满意度患者{i}", id_card=f"33038219800101{i:04d}")
                    for i in range(4)]
        db.add_all(patients)
        db.flush()
        pids = [p.id for p in patients]
        db.execute(insert(SatisfactionSurvey), [
            {"target_type": kind, "target_id": n + 1, "patient_id": pids[i], "score": score, "comment": f"评语{n}"}
            for n, (i, kind, score) in enumerate(SURVEYS)])
        db.commit()
    return pids


def _原算法(db, *, target_type=None, max_score=None, offset=0, limit=100) -> list[dict]:
    """修前的写法原样搬来当判据：整张患者表的（id, 姓名）读出来对。"""
    query = db.query(SatisfactionSurvey)
    if target_type:
        query = query.filter(SatisfactionSurvey.target_type == target_type)
    if max_score is not None:
        query = query.filter(SatisfactionSurvey.score <= max_score)
    rows = query.order_by(SatisfactionSurvey.id.desc()).offset(offset).limit(limit).all()
    names = {pid: name for pid, name in db.query(Patient.id, Patient.name).all()}
    return [{"id": s.id, "target_type": s.target_type, "target_id": s.target_id, "patient_id": s.patient_id,
             "patient_name": names.get(s.patient_id, ""), "score": s.score, "comment": s.comment,
             "date": s.created_at.date().isoformat()} for s in rows]


@pytest.mark.parametrize("params", [
    {"limit": 3}, {"offset": 3, "limit": 3}, {"max_score": 2}, {"target_type": "encounter"},
    {"target_type": "nothing"},
])
def test_特征化_逐行与原算法相同(client, admin, world, params):
    resp = client.get("/api/surveys", params=params, headers=admin)
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        assert resp.json() == _原算法(db, **params)


def test_特征化_姓名配得上(client, admin, world):
    body = client.get("/api/surveys", params={"max_score": 2}, headers=admin).json()
    assert [(r["patient_name"], r["score"]) for r in body] == [("满意度患者2", 2), ("满意度患者0", 1), ("满意度患者1", 2)]


def _rows_fetched(client, admin, url: str, params: dict) -> int:
    """这一个请求里全部 SELECT 取回的行数：拦下每条语句与参数，请求结束后在同一份数据上重放一遍数行数。"""
    seen: list[tuple] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get(url, params=params, headers=admin)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text
    assert seen, "没拦到任何 SELECT（用例失效，请检查拦截方式）"
    with engine.connect() as conn:
        return sum(len(conn.exec_driver_sql(statement, parameters).fetchall()) for statement, parameters in seen)


@pytest.mark.parametrize("params", [{"limit": 3}, {"target_type": "nothing"}])
def test_患者翻几番_取回行数不变(client, admin, world, params):
    before = _rows_fetched(client, admin, "/api/surveys", params)
    with SessionLocal() as db:
        start = db.query(Patient).count()
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-SVMORE-{start + i}", "name": f"没有评价的患者{start + i}",
             "id_card": f"33038219900202{start + i:04d}"} for i in range(120)])
        db.commit()
    after = _rows_fetched(client, admin, "/api/surveys", params)
    assert after == before, f"多了 120 个没有评价的患者，请求多取回了 {after - before} 行——还在把整张患者表读进内存配姓名"
