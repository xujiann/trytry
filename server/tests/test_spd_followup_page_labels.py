"""智能随访页三处文案与功能对不上（P2-1638，第四十八批扫描 AL1-8）。

修前（纯前端取值，按代码读）：
- 呼叫台账「来源」列原样印 `followup` / `revisit`（`pages-spd.js` 的 `esc(c.ref_type)`），也看不出是哪条随访——出参只有
  引用号，没有中文、没有计划日（P2-672 只提到呼叫清单不显示计划日）；
- 抽查结论 `warn` 页面写「提醒」，`spd_qc_samples.result` 的列注释是「基本合格」，两者意思不同；
- 抽查表单只有比例，接口早有 `count`（P2-640 修过「按数量补到 count」），需求对照表 #11 写「按比例或数量」，页面上用不到。

修法：呼叫台账出参末尾追加 `ref_type_name`（中文，表外的码原样）与 `ref_plan_date`（被引用的随访 / 复诊的计划日，取不到
为空串；按页一次取，不逐行查），页面印中文、引用号与计划日；`warn` 的说法以列注释为准，用例把页面映射对着列注释比；
抽查表单补「按数量」一栏，填了只送 count、没填只送 ratio（二选一，照接口的校验）。
"""
import re
from pathlib import Path

import pytest

from app import clock

B = "/api/spd"
ROOT = Path(__file__).resolve().parents[1] / "app"
PAGE = (ROOT / "static" / "pages-spd.js").read_text(encoding="utf-8")
MODELS = (ROOT / "spd" / "models.py").read_text(encoding="utf-8")


# ================================================================ 呼叫台账：中文来源与计划日
@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdCallTask, SpdFollowupRecord, SpdRevisit

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21638 患者", "id_card": "330106197001011638", "phone": "13900016380"}).json()["id"]
    with SessionLocal() as db:
        records = [SpdFollowupRecord(patient_id=patient, planned_at=day, status="planned")
                   for day in ("2026-11-01", "2026-11-08", "2026-11-15")]
        revisit = SpdRevisit(patient_id=patient, plan_date="2026-12-01", status="planned")
        db.add_all([*records, revisit])
        db.flush()
        refs = [("followup", r.id) for r in records] + [("revisit", revisit.id), ("edu", 99), ("followup", None),
                                                         ("followup", 987654)]   # 悬空的引用号
        calls = [SpdCallTask(patient_id=patient, phone="13900016380", ref_type=t, ref_id=i, status="connected")
                 for t, i in refs]
        db.add_all(calls)
        db.commit()
        return {"patient": patient, "calls": {c.id: (c.ref_type, c.ref_id) for c in calls},
                "records": [r.id for r in records], "revisit": revisit.id}


def test_呼叫台账出参带中文来源与计划日(client, admin, world):
    rows = client.get(f"{B}/call-tasks", headers=admin, params={"patient_name": "P21638"}).json()
    got = {r["id"]: (r["ref_type"], r["ref_id"], r["ref_type_name"], r["ref_plan_date"]) for r in rows}
    records, revisit = world["records"], world["revisit"]
    assert sorted(got.values(), key=str) == sorted([
        ("followup", records[0], "随访", "2026-11-01"), ("followup", records[1], "随访", "2026-11-08"),
        ("followup", records[2], "随访", "2026-11-15"), ("revisit", revisit, "复诊", "2026-12-01"),
        ("edu", 99, "edu", ""),                 # 表外的来源码原样；不认识的来源不去猜计划日
        ("followup", None, "随访", ""),          # 没挂引用号
        ("followup", 987654, "随访", ""),        # 引用的随访已不在
    ], key=str)
    assert list(rows[0].keys())[-2:] == ["ref_type_name", "ref_plan_date"]   # 末尾追加，原有键序不动


def test_计划日按页一次取_不逐行查(client, admin, world):
    from sqlalchemy import event

    from app.database import engine

    seen: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        resp = client.get(f"{B}/call-tasks", headers=admin, params={"patient_name": "P21638"})
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert resp.status_code == 200 and len(resp.json()) == 7
    reads = [s for s in seen if s.lstrip().upper().startswith("SELECT") and "FROM spd_followup_records" in s]
    assert len(reads) == 1, reads   # 一页 5 条随访呼叫，只查一次随访记录


# ================================================================ 页面
def test_呼叫台账来源列印中文与计划日_不再印原码():
    start = PAGE.index('${panel("呼叫任务与录音"')
    ledger = PAGE[start:PAGE.index("`)}`;", start)]
    assert "<td>${esc(c.ref_type)}</td>" not in ledger   # 修前原样印 followup / revisit
    assert "esc(c.ref_type_name || c.ref_type)" in ledger and "esc(c.ref_plan_date)" in ledger


def test_抽查结论的说法与列注释一致():
    comment = re.search(r"# (pass=.*)\n\s*result: Mapped\[str\]", MODELS[MODELS.index("class SpdQcSample("):])
    assert comment, "spd_qc_samples.result 的列注释找不到了"
    expected = dict(part.split("=", 1) for part in comment.group(1).split(", "))
    assert expected == {"pass": "合格", "warn": "基本合格", "fail": "不合格"}
    page_map = re.search(r"^const SPD_QC_RESULT = \{(.*)\};$", PAGE, re.M).group(1)
    labels = dict(re.findall(r'(\w+): \["([^"]+)"', page_map))
    assert {k: labels[k] for k in expected} == expected   # 修前 warn 是「提醒」


def test_抽查表单能按数量提交_与比例二选一():
    form = PAGE[PAGE.index('<form class="inline" id="spd-qc-form"'):]
    form = form[:form.index("</form>")]
    assert re.search(r'<input name="count" type="number" min="1" max="500"', form)   # 修前只有比例
    submit = PAGE[PAGE.index('$("#spd-qc-form").onsubmit'):]
    submit = submit[:submit.index("\n  };\n")]
    assert 'formJson(e.target, ["ratio", "count"])' in submit
    assert "if (body.count) delete body.ratio;" in submit and "else delete body.count;" in submit


def test_接口按数量抽_比例不送也行(client, admin, world):
    """页面填了数量只送 count（不送 ratio）：接口照 count 抽，缺省比例不挡。"""
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        for _ in range(3):
            db.add(SpdFollowupRecord(patient_id=world["patient"], planned_at=clock.today().isoformat(),
                                     status="done", dept="P21638质控科"))
        db.commit()
    resp = client.post(f"{B}/qc-samples/plan", headers=admin,
                       json={"dept": "P21638质控科", "count": 2, "batch": "P21638"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["pool"], resp.json()["planned"], resp.json()["created"]) == (3, 2, 2)
