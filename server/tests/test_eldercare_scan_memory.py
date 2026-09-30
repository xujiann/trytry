"""老年健康三个接口（失能清单 / 预警 / 统计）取「每人最近一次评估」不再整行载入全部评估（P2-1154，第三十三批扫描 A4-3）。

修前：三个接口共用的 `_latest_by_patient` 吃的是 `db.query(ElderlyAssessment)….all()`——全部评估整行载入成 ORM 对象
再挑每人最近一次，页面还并发调三个。评估从 1 万到 3 万条（老人 1 万）每个接口载入 10,001 → 30,001 个对象、峰值
15 → 46 MB，内存与耗时都随评估条数线性涨。修后照 P2-8 第四批 spd `assessment_stats` 的修法：`yield_per` 流式扫、
只取要读的列，留在内存里的只有每人一行；判定一字不改。清单与预警要不要分页、按机构收口待裁定，本文件不碰。

两段分工（与 `test_spd_stats_truncation.py` 同一套做法）：
- `test_特征化_*`：三个接口的响应逐字节等于照**原实现**（整行载入 + 同一判定）算出来的——修前修后都绿，钉「没改行为」；
- `test_不整行载入_*`：ORM 载入数为 0、评估条数翻四倍（老人数不变）峰值内存不跟着涨——修前红，钉「修了什么」。
"""
import json
import tracemalloc
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, func, insert

from app import clock
from app.database import SessionLocal
from app.datetypes import legacy_date
from app.models import ElderlyAssessment, Patient

TODAY = "2026-09-30"
PATHS = ("/api/eldercare/disabled", f"/api/eldercare/alerts?today={TODAY}", "/api/eldercare/stats")
LEVELS = ("能力完好", "轻度失能", "中度失能", "重度失能", "")
CALIBER = "按每位老人最近一次评估统计（非评估条数）；认知筛查与体质辨识未做的单列，不按 0 分并入"


def _assessed_date(i: int, stale: bool) -> str:
    """覆盖：没填（按录入那天兜底）、存量斜杠 / 八位数字写法、读不成的文字、规范日期（含同日并列、超一年）。"""
    if i % 11 == 0:
        return ""
    if i % 13 == 0:
        return "2024/01/15" if stale else "2026/01/15"
    if i % 17 == 0:
        return "20240301" if stale else "20260301"
    if i % 19 == 0:
        return "不详"
    return (datetime(2026, 9, 30) - timedelta(days=(i * 53) % 300 + (400 if stale else 0))).date().isoformat()


def _add_assessments(pids: list[int], start: int, stop: int) -> None:
    rows = []
    for i in range(start, stop):
        # 按 id 与按 patient_id 两种次序下各人第一次出现的先后不同：键序（响应里的先后）两种都要钉住
        slot = (i * 7) % len(pids)
        stale = slot % 6 == 0   # 这几位一年多没评过：出复评提醒
        rows.append({
            "patient_id": pids[slot],
            "adl_score": (i * 37) % 101,
            "cognitive_score": 0 if i % 5 == 0 else (i * 3) % 31,
            "tcm_constitution": ("", "平和质", "气虚质")[i % 3],
            "care_level": LEVELS[(i * 3) % len(LEVELS)],
            "assessed_date": _assessed_date(i, stale),
            # 录入时刻铺开在 UTC 0–23 点：东八区 0–8 点录的换成本地日期后是另一天（没填日期的兜底）
            "created_at": datetime(2026, 9, 30, 23) - timedelta(days=(i * 29) % 300 + (400 if stale else 0),
                                                              hours=(i * 5) % 24),
        })
    with SessionLocal() as db:
        db.execute(insert(ElderlyAssessment), rows)
        db.commit()


@pytest.fixture(scope="module")
def pids(client):
    with SessionLocal() as db:
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-ELD-SCAN-{i:03d}", "name": f"老年扫描{i}", "id_card": f"33019919400101{i:04d}",
             "gender": "男" if i % 2 else "女", "birth_date": "1940-01-01", "phone": ""}
            for i in range(60)
        ])
        db.commit()
        ids = [pid for (pid,) in db.query(Patient.id).filter(Patient.ehc_no.like("EHC-ELD-SCAN-%")).order_by(Patient.id)]
    _add_assessments(ids, 0, 400)
    return ids


def _original(today: str) -> dict[str, object]:
    """原实现（P2-1154 之前）逐字照抄：整行载入全部评估，挑每人最近一次，再按三个接口各自的口径出参。"""
    def assessed_on(row):
        return legacy_date(row.assessed_date) or clock.to_local(row.created_at).date().isoformat()

    def latest_by_patient(rows):
        latest = {}
        for row in rows:
            current = latest.get(row.patient_id)
            if current is None or assessed_on(row) >= assessed_on(current):
                latest[row.patient_id] = row
        return latest

    with SessionLocal() as db:
        by_id = latest_by_patient(db.query(ElderlyAssessment).order_by(ElderlyAssessment.id).all())
        disabled = [
            {"patient_id": a.patient_id, "care_level": a.care_level, "adl_score": a.adl_score}
            for a in by_id.values() if a.care_level != "能力完好"
        ]
        current = datetime.strptime(today, "%Y-%m-%d").date()
        reassess_before = current.replace(year=current.year - 1).isoformat()
        alerts = []
        for a in by_id.values():
            if a.care_level == "重度失能":
                alerts.append({"patient_id": a.patient_id, "alert_type": "severe_disability",
                               "message": "重度失能，建议纳入家庭病床/上门服务专案", "assessed_date": a.assessed_date})
            if assessed_on(a) <= reassess_before:
                alerts.append({"patient_id": a.patient_id, "alert_type": "reassess_due",
                               "message": "距上次健康评估已超一年，应安排复评", "assessed_date": a.assessed_date})
        rows = db.query(ElderlyAssessment).order_by(ElderlyAssessment.patient_id, ElderlyAssessment.id).all()
        latest = latest_by_patient(rows)
        by_level: dict[str, int] = {}
        cognitive_scores, tcm_done = [], 0
        for r in latest.values():
            by_level[r.care_level] = by_level.get(r.care_level, 0) + 1
            if r.cognitive_score > 0:
                cognitive_scores.append(r.cognitive_score)
            if r.tcm_constitution:
                tcm_done += 1
        people = len(latest)
        disabled_count = people - by_level.get("能力完好", 0)
        stats = {
            "assessed_people": people,
            "assessment_records": len(rows),
            "by_care_level": by_level,
            "disabled_count": disabled_count,
            "disabled_rate_pct": round(disabled_count * 100 / people, 2) if people else None,
            "cognitive": {
                "screened": len(cognitive_scores),
                "unscreened": people - len(cognitive_scores),
                "avg_score": round(sum(cognitive_scores) / len(cognitive_scores), 1) if cognitive_scores else None,
            },
            "tcm_constitution": {"done": tcm_done, "not_done": people - tcm_done},
            "caliber": CALIBER,
        }
    return {PATHS[0]: disabled, PATHS[1]: {"total": len(alerts), "alerts": alerts}, PATHS[2]: stats}


def _assert_same_as_original(client, admin):
    expected = _original(TODAY)
    for path in PATHS:
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, resp.text
        # json.dumps 保留键序：先后（失能清单 / 预警按各人第一次评估的编号、失能构成按患者编号）一并比
        assert json.dumps(resp.json(), ensure_ascii=False) == json.dumps(expected[path], ensure_ascii=False), path


def test_特征化_三个接口与原实现逐字节一致(client, admin, pids):
    expected = _original(TODAY)
    # 数据确实覆盖到了要比的分支：同一人多次评估且有同日并列、失能与完好都有、两类预警都有
    assert expected[PATHS[2]]["assessed_people"] == len(pids)
    assert expected[PATHS[2]]["assessment_records"] == 400
    assert {a["alert_type"] for a in expected[PATHS[1]]["alerts"]} == {"severe_disability", "reassess_due"}
    assert 0 < len(expected[PATHS[0]]) < len(pids)
    with SessionLocal() as db:
        ties = (
            db.query(ElderlyAssessment.patient_id).filter(ElderlyAssessment.assessed_date != "")
            .group_by(ElderlyAssessment.patient_id, ElderlyAssessment.assessed_date)
            .having(func.count() > 1).count()
        )
    assert ties > 0
    _assert_same_as_original(client, admin)


def test_不整行载入_三个接口ORM载入数为0(client, admin, pids):
    """修前每个接口把全部评估载入成 ORM 对象（这里 400 条，各载入 400 个）。"""
    loaded = []

    def _on_load(target, context):
        loaded.append(target)

    event.listen(ElderlyAssessment, "load", _on_load)
    try:
        for path in PATHS:
            assert client.get(path, headers=admin).status_code == 200
    finally:
        event.remove(ElderlyAssessment, "load", _on_load)
    assert len(loaded) == 0, f"三个接口共载入 {len(loaded)} 个评估 ORM 对象——又整行载入全部评估了"


def _peak_bytes(client, admin, path):
    client.get(path, headers=admin)   # 预热：编译缓存等一次性开销不算进峰值
    tracemalloc.start()
    try:
        assert client.get(path, headers=admin).status_code == 200
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_不整行载入_评估条数翻四倍峰值内存不跟着涨(client, admin, pids):
    """老人数不变、评估从 3,000 条加到 12,000 条：修前峰值随条数线性涨（每条约 1.5 KB 的 ORM 对象），
    修后内存里只留每人一行加一两批流式缓冲，峰值由老人数与批大小决定。结果仍与原实现逐字节一致。

    起点取 3,000 条而不是更少：流式每批 1,000 行，取下一批时上一批还没放掉，不足两批时峰值还没到顶。"""
    _add_assessments(pids, 400, 3000)
    small = {path: _peak_bytes(client, admin, path) for path in PATHS}
    _add_assessments(pids, 3000, 12000)
    big = {path: _peak_bytes(client, admin, path) for path in PATHS}
    for path in PATHS:
        assert big[path] < small[path] * 1.25, (
            f"{path}：评估 3,000 → 12,000 条（老人数不变），峰值 {small[path] / 1e6:.2f} → {big[path] / 1e6:.2f} MB"
            "——内存又随评估条数涨了"
        )
    _assert_same_as_original(client, admin)
