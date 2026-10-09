"""评估没写病种、量表也是通用的：只在管一个病种的按它回写（P1-252，第四十七批扫描 AK2-1）。

评估页提交原先只送患者、量表编码与版本、作答，`create_assessment` 的病种取「请求的或量表的」——个案管理师手册指定的
综合风险评估量表（种子 `assess_risk_common`）是通用量表（病种空），于是评估记录的病种是空串，`_managed_enrollment_of`
遇空病种直接取不到档案。2026-10-09 实测（修前代码）：照页面送法评出 18 分极高危，档案仍是低危、不开 14 天高危复诊、不开
自动干预；按病种的「待评估」清单（`GET /enrollments?program_code=&pending=assess`）照旧列着这位患者，按病种跑分与
`/assessments/stats?program_code=` 也不计这次评估。

修法与干预 / 复诊 / 转诊的 `enrollment_for` 同一句（P1-139）：两头都没写病种的，只在管一个病种的挂它、照常回写与派发；
在管几个病种的不替人猜、没有在管档案的无从回写，都只存评估，回执追加 `writeback_note` 说明原因（条件键，其余回执逐字节
不变）。评估表单补一个病种下拉（留空随量表 / 只在管一个病种的按它）。不按量表类别分流（P2-868 待裁定）。
"""
from datetime import timedelta
from pathlib import Path

import pytest

from app import clock

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 综合风险量表的满分答法：4+4+6+4=18 ≥ 14 → very_high
VERY_HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "2项及以上", "selfcare": "完全依赖"}

#: 清单行（`CareAssessmentOut`）的键：推出了病种 / 写了病种的回执就是这一组，不多不少
ROW_KEYS = {"id", "patient_id", "patient_name", "scale_id", "scale_code", "scale_version", "program_code", "answers",
            "score", "risk_level", "advice", "channel", "created_at"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1252 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    scale = next(s for s in client.get(f"{B}/catalog", headers=admin).json()["scales"]
                 if s["code"] == "assess_risk_common")
    assert scale["program_code"] == ""   # 前提：手册指定的综合风险评估量表是通用量表
    return {"org": org, "scale": scale}


def _patient(client, admin, name, id_card, programs, org):
    pid = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card}).json()["id"]
    enrollments = {}
    for code in programs:
        made = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": pid, "program_code": code, "org_id": org, "risk_level": "low"})
        assert made.status_code == 201, made.text
        enrollments[code] = made.json()["id"]
    return pid, enrollments


def _page_assess(client, admin, world, pid, **extra):
    """照评估页的送法：患者、量表编码与版本、作答（病种下拉留空时不送）。"""
    return client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": pid, "scale_code": "assess_risk_common", "scale_id": world["scale"]["id"],
        "answers": VERY_HIGH_ANSWERS, **extra})


def _risk(enrollment_id):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).risk_level


def _high_risk_revisits(client, admin, pid):
    return [(r["program_code"], r["plan_date"]) for r in client.get(
        f"{B}/revisits", headers=admin, params={"patient_id": pid}).json() if r["source"] == "high_risk"]


def _pending_assess(client, admin, world, program):
    return [e["patient_id"] for e in client.get(f"{B}/enrollments", headers=admin, params={
        "program_code": program, "pending": "assess", "org_id": world["org"]}).json()]


def test_页面送法_只在管一个病种的按它回写_开高危复诊_不再挂待评估(client, admin, world):
    pid, enrollments = _patient(client, admin, "P1252 单病种", "330127197001011252", ["hypertension"], world["org"])
    assert pid in _pending_assess(client, admin, world, "hypertension")

    resp = _page_assess(client, admin, world, pid)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert (out["risk_level"], out["program_code"]) == ("very_high", "hypertension"), out   # 修前病种是空串
    assert set(out) == ROW_KEYS, out   # 推出来了：回执不带 writeback_note，原有键一个不多
    assert _risk(enrollments["hypertension"]) == "very_high"   # 修前仍是 low
    plan = (clock.today() + timedelta(days=14)).isoformat()
    assert _high_risk_revisits(client, admin, pid) == [("hypertension", plan)]   # 修前一条也没有
    assert pid not in _pending_assess(client, admin, world, "hypertension")   # 修前照旧挂在待评估里


def test_在管两个病种又不写病种_不替人猜_回执写明没回写(client, admin, world):
    pid, enrollments = _patient(client, admin, "P1252 双病种", "330127197001021252", ["hypertension", "diabetes"],
                                world["org"])
    resp = _page_assess(client, admin, world, pid)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["program_code"] == "" and "不止一个病种" in out["writeback_note"], out
    assert set(out) == ROW_KEYS | {"writeback_note"}
    assert (_risk(enrollments["hypertension"]), _risk(enrollments["diabetes"])) == ("low", "low")
    assert _high_risk_revisits(client, admin, pid) == []

    # 写了病种的照旧：只回写这个病种的档案，回执不带说明
    picked = _page_assess(client, admin, world, pid, program_code="diabetes")
    assert picked.status_code == 201, picked.text
    assert picked.json()["program_code"] == "diabetes" and "writeback_note" not in picked.json(), picked.json()
    assert (_risk(enrollments["hypertension"]), _risk(enrollments["diabetes"])) == ("low", "very_high")
    assert [r[0] for r in _high_risk_revisits(client, admin, pid)] == ["diabetes"]


def test_没有在管档案_只存评估_回执写明(client, admin, world):
    pid, _ = _patient(client, admin, "P1252 未纳管", "330127197001031252", [], world["org"])
    resp = _page_assess(client, admin, world, pid)
    assert resp.status_code == 201, resp.text
    assert resp.json()["program_code"] == "" and "没有在管" in resp.json()["writeback_note"], resp.json()
    assert _high_risk_revisits(client, admin, pid) == []


def test_评估表单有病种下拉_提交带上_回执说明跟在结论后面():
    spd = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    form = spd[spd.index('<form class="inline" id="spd-assess-form">'):]
    form = form[:form.index("</form>")]
    assert '<select name="program_code"><option value="">病种：' in form, form   # 修前表单没有病种
    handler = spd[spd.index('$("#spd-assess-form").onsubmit'):]
    handler = handler[:handler.index('$("#spd-assess-query").onsubmit')]
    assert "program_code: picked.program_code" in handler, handler   # 修前只送患者、量表、作答
    assert "r.writeback_note" in handler, handler
