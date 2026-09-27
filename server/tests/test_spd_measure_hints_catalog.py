"""成员端「监测数据录入」的指标提示取自后端指标目录（P2-643，第十四批「单位与量纲」扫描 R3-6）。

提示列表原先是前端自抄的一份，把餐后 2 小时血糖、尿酸抄成 `glucose_post` / `uric_acid`；后端的指标目录
（`service.MEASURE_FIELDS`，规则字段 `rules.FIELD_SOURCES` 同名）是 `glucose_pp2h` / `ua`。照提示录进去的值落库照收
（后端不限指标编码），事实字典与纳入 / 转诊规则永远读不到：餐后血糖 16.8 的患者，按 `glucose_pp2h > 11.1` 写的
转诊规则不命中，按编码看趋势也看不到。`GET /api/spd/meta` 的文档早写着「做成接口而不是前端写死……两处各维护一份的
结果一定是前端能选、后端不认」——提示列表正是漏掉的那一份。

修法：元数据接口加 `measure_fields`（取 MEASURE_FIELDS、名称取 FIELD_SOURCES），成员端的 datalist 用它，前端不再抄。
提示仍**不是白名单**——后端照旧收任意指标编码、按管理目标判级。
"""
import re
from pathlib import Path

from app.database import SessionLocal
from app.spd.rules import FIELD_SOURCES
from app.spd.service import MEASURE_FIELDS, build_facts

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_元数据接口给出监测指标目录_名称取规则字段(client, admin):
    body = client.get("/api/spd/meta", headers=admin).json()
    assert body["measure_fields"] == [{"key": m, "name": FIELD_SOURCES[m]} for m in MEASURE_FIELDS]
    keys = {x["key"] for x in body["measure_fields"]}
    assert {"glucose_pp2h", "ua"} <= keys and not {"glucose_post", "uric_acid"} & keys   # 修前没有这一组


def test_成员端提示取接口_前端不再自抄():
    source = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    start = source.index("async function renderSpdMember()")
    body = source[start:source.index("\nasync function ", start + 1)]
    datalist = body[body.index('<datalist id="spd-metric-hints">'):]
    datalist = datalist[:datalist.index("</datalist>")]
    assert "meta.measure_fields.map" in datalist, datalist   # 修前是前端常量 SPD_METRIC_HINTS
    assert "spdMeta()" in body


def test_前端不再出现后端不认的两个编码():
    hits = [f"{path.relative_to(STATIC)}:{no}" for path in sorted(STATIC.rglob("*.js"))
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\b(glucose_post|uric_acid)\b", line)]
    assert hits == []   # 修前 pages-spd.js 的提示常量里各有一处


def test_按目录编码录的值规则读得到_按旧提示录的读不到(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2643 糖尿病患者", "id_card": "330281196602062643", "gender": "男", "birth_date": "1966-02-06"}).json()
    for metric, value in (("glucose_pp2h", 16.8), ("glucose_post", 17.2), ("ua", 520.0), ("uric_acid", 530.0)):
        got = client.post("/api/spd/measurements", headers=admin, json={
            "patient_id": patient["id"], "metric": metric, "value": value})
        assert got.status_code in (200, 201), got.text   # 后端不限编码：旧提示的值也照收，只是没人读
    with SessionLocal() as db:
        facts = build_facts(db, patient["id"])
    assert (facts.get("glucose_pp2h"), facts.get("ua")) == (16.8, 520.0)
    assert "glucose_post" not in facts and "uric_acid" not in facts
