"""老年健康「每人最近一次评估」按日历读评估日期，存量非规范写法不再压过规范日期（P2-890，第二十四批「存量行 vs 新规则」扫描 Z3-3）。

P2-125 把「最近一次」改成按评估日期取最晚的，比的是字符串；P1-61 之前页面是自由文本框，存量里的「2026/01/15」「20260301」
在同一年里比任何规范日期都「晚」（'/'、'0' 都大于 '-'）：
- 乙：1 月的「重度失能」旧表（2026/01/15）压住 9 月复评的「能力完好」——人还在失能清单里、还报重度失能专案；
- 丙：3 月的「能力完好」旧表（20260301）压住 9 月复评的「重度失能」——失能清单、预警里都没有她，漏报；
- 甲：对照，存量是规范写法，结果正确。
修后按日历读（`datetypes.legacy_date`：斜杠、点号、不补零、八位数字、全角写法照读），读不成的与没填同一个兜底（录入那天）。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.datetypes import legacy_date
from app.models import ElderlyAssessment


@pytest.fixture(scope="module")
def elders(client, admin):
    ids = {}
    for key, card in (("甲", "110101194001012890"), ("乙", "110101194102022890"), ("丙", "110101194203032890")):
        made = client.post("/api/patients", headers=admin, json={"name": f"P2890 老人{key}", "id_card": card})
        assert made.status_code in (200, 201), made.text
        ids[key] = made.json()["id"]
    with SessionLocal() as db:   # P1-61 之前经自由文本框录进去的评估
        db.add_all([
            ElderlyAssessment(patient_id=ids["甲"], adl_score=30, care_level="重度失能", assessed_date="2026-01-15",
                              created_at=datetime(2026, 1, 15, 2)),
            ElderlyAssessment(patient_id=ids["乙"], adl_score=30, care_level="重度失能", assessed_date="2026/01/15",
                              created_at=datetime(2026, 1, 15, 2)),
            ElderlyAssessment(patient_id=ids["丙"], adl_score=96, care_level="能力完好", assessed_date="20260301",
                              created_at=datetime(2026, 3, 1, 2)),
        ])
        db.commit()
    for key, score in (("甲", 96), ("乙", 96), ("丙", 30)):   # 9 月复评（规范写法）
        got = client.post("/api/eldercare/assessments", headers=admin,
                          json={"patient_id": ids[key], "adl_score": score, "assessed_date": "2026-09-20"})
        assert got.status_code == 201, got.text
    return ids


def test_失能清单与预警按9月复评算(client, admin, elders):
    names = {v: k for k, v in elders.items()}
    disabled = {names[d["patient_id"]]: d["care_level"] for d in client.get("/api/eldercare/disabled", headers=admin).json()
                if d["patient_id"] in names}
    assert disabled == {"丙": "重度失能"}   # 修前 {'乙': '重度失能'}：乙被旧表留在清单里、丙漏掉
    alerts = client.get("/api/eldercare/alerts", headers=admin, params={"today": "2026-09-29"}).json()["alerts"]
    severe = sorted(names[a["patient_id"]] for a in alerts
                    if a["patient_id"] in names and a["alert_type"] == "severe_disability")
    assert severe == ["丙"]   # 修前 ['乙']


@pytest.mark.parametrize(("stored", "read"), [
    ("2026-01-15", "2026-01-15"), ("2026/01/15", "2026-01-15"), ("2026.1.5", "2026-01-05"), ("2026-1-15", "2026-01-15"),
    ("20260301", "2026-03-01"), ("２０２６-０１-１５", "2026-01-15"),
    ("", None), ("上午", None), ("2026-02-30", None), ("2026/13/01", None),
])
def test_存量日期的读法(stored, read):
    assert legacy_date(stored) == read
