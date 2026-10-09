"""门急诊文书完整性不看门诊病历与诊断：写了病历的就诊和什么都没写的就诊，结果一模一样（P2-1632，第四十八批扫描 AL4-2）。

修前（实测）：E1 写了门诊病历（201，乙级）、E2 什么都没写，`/api/outpatient/encounters/{id}/completeness` 两者都回
`treatment_records 0 / nursing_records 0 / consents_* 0`——出参只数处置、护理、告知书三类。同一函数的 docstring 自己写着
「感冒开药就该只有病历」，注释写「完整性清单会暴露该次就诊有哪些文书（病历/处置/知情同意）」，病历却一项都不数；全仓也没有
别处列出「有就诊、没写病历」的就诊（qc-summary 只统计已写的病历）。

修法（出参只加不改）：末尾追加 `medical_record`（0 / 1，一次就诊一份）、`medical_record_grade`（环节质控等级，没写为空串）
与 `has_diagnosis`（就诊上有没有诊断编码或名称）；note 文案同步；文书页完整性面板加「门诊病历」「诊断」两张卡片。
既有键的值不变（note 除外：文案按条目同步改）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 修前的键与次序，再接 P2-1631 追加的三个认人键——本条的新键只许接在它们后面
BEFORE_KEYS = ["encounter_id", "patient_id", "treatment_records", "nursing_records", "consents_total",
               "consents_pending", "consents_refused", "note", "patient_name", "encounter_created_at", "org_name"]
NEW_KEYS = ["medical_record", "medical_record_grade", "has_diagnosis"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21632 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21632_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "王医生"})
    assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21632 张三", "id_card": "330106197001011632", "gender": "男"}).json()["id"]
    return {"org": org, "patient": patient, "doctor": login(client, "p21632_doc", "passw0rd1")}


def _encounter(client, world, **extra) -> int:
    r = client.post("/api/encounters", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _completeness(client, world, encounter_id: int) -> dict:
    r = client.get(f"/api/outpatient/encounters/{encounter_id}/completeness", headers=world["doctor"])
    assert r.status_code == 200, r.text
    return r.json()


def test_写了门诊病历的为1带质控等级_没写的为0(client, world):
    written, blank = _encounter(client, world), _encounter(client, world)
    rec = client.post("/api/quality/records", headers=world["doctor"], json={
        "encounter_id": written, "chief_complaint": "咳嗽3天", "present_illness": "3天前受凉后咳嗽，无发热",
        "past_history": "无", "physical_exam": "咽红，双肺呼吸音清", "diagnosis_basis": "症状体征",
        "treatment_plan": "对症治疗"})
    assert rec.status_code == 201, rec.text
    a, b = _completeness(client, world, written), _completeness(client, world, blank)
    assert (a["medical_record"], a["medical_record_grade"]) == (1, rec.json()["record"]["qc_grade"])   # 修前没有这一项
    assert (b["medical_record"], b["medical_record_grade"]) == (0, "")
    # 修前两次就诊除了编号一模一样
    strip = {"encounter_id", "encounter_created_at"}
    assert {k: v for k, v in a.items() if k not in strip} != {k: v for k, v in b.items() if k not in strip}


def test_就诊上有诊断编码或名称才算有诊断(client, world):
    cases = [({}, False), ({"diagnosis_name": "急性上呼吸道感染"}, True), ({"diagnosis_code": "J06.900"}, True),
             ({"diagnosis_code": "J06.900", "diagnosis_name": "急性上呼吸道感染"}, True)]
    for extra, expected in cases:
        assert _completeness(client, world, _encounter(client, world, **extra))["has_diagnosis"] is expected, extra


def test_既有键的值不变_新键接在末尾(client, world):
    """特征化：处置 1 条、护理 1 条、挂在本次就诊上的告知书 1 份待签——既有七个计数键与患者号照旧，note 之外原样。"""
    enc = _encounter(client, world, diagnosis_name="急性胃肠炎")
    doctor = world["doctor"]
    assert client.post(f"/api/outpatient/encounters/{enc}/treatments", headers=doctor,
                       json={"treatment_name": "静脉输液"}).status_code == 201
    assert client.post(f"/api/outpatient/encounters/{enc}/nursing-records", headers=doctor,
                       json={"content": "输液中滴速40滴/分"}).status_code == 201
    consent = client.post("/api/outpatient/consents", headers=doctor, json={
        "patient_id": world["patient"], "org_id": world["org"], "consent_type": "treatment", "title": "输液告知",
        "content": "输液可能出现静脉炎", "related_type": "encounter", "related_id": enc})
    assert consent.status_code == 201, consent.text
    body = _completeness(client, world, enc)
    assert list(body) == BEFORE_KEYS + NEW_KEYS
    assert {k: body[k] for k in BEFORE_KEYS[:7]} == {
        "encounter_id": enc, "patient_id": world["patient"], "treatment_records": 1, "nursing_records": 1,
        "consents_total": 1, "consents_pending": 1, "consents_refused": 0}
    assert (body["medical_record"], body["medical_record_grade"], body["has_diagnosis"]) == (0, "", True)


def test_note_写明病历与诊断两项(client, world):
    note = _completeness(client, world, _encounter(client, world))["note"]
    assert "门诊病历" in note and "诊断" in note
    assert "不判合格与否" in note and "待签署的告知书应在就诊结束前处理完毕" in note   # 原有两句照旧


# ---------- 页面：完整性面板加两张卡片 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


_HARNESS = """
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "",
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const DATA = JSON.parse(process.argv[1]);
globalThis.localStorage = { getItem: (k) => (k === "medplat_od_encounter" ? "2" : null), setItem() {}, removeItem() {} };
async function api(path) {
  if (path === "/api/outpatient/encounters/2/completeness") return DATA;
  return [];
}
async function route() {}
"""


def _render(completeness: dict) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(") + _top_level(core, "function currentRole(", "\n")
              + _top_level(core, "function encounterWho(")
              + _top_level(public, "const NURSING_LEVELS", "\n") + _top_level(mgmt, "const TPL_STATUS", "\n")
              + _top_level(mgmt, "const CONSENT_TYPES", "};\n") + _top_level(mgmt, "const CONSENT_RELATIONS", "\n")
              + _top_level(mgmt, "async function renderOutpatientDocs(")
              + "(async () => { await renderOutpatientDocs();\n"
              "  process.stdout.write(JSON.stringify(els['#page-body'].innerHTML)); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps(completeness, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


BASE = {"encounter_id": 2, "patient_id": 2, "treatment_records": 0, "nursing_records": 0, "consents_total": 0,
        "consents_pending": 0, "consents_refused": 0, "note": "只列事实", "patient_name": "张三",
        "encounter_created_at": "2026-10-09T04:52:31", "org_name": "甲镇卫生院"}


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_完整性面板有门诊病历与诊断两张卡片():
    html = _render({**BASE, "medical_record": 1, "medical_record_grade": "乙<", "has_diagnosis": True})
    assert '<div class="card"><span class="k">门诊病历</span><b>已写（乙&lt;级）</b></div>' in html   # 修前没有这张卡片
    assert '<div class="card"><span class="k">诊断</span><b>有</b></div>' in html
    html = _render({**BASE, "medical_record": 0, "medical_record_grade": "", "has_diagnosis": False})
    assert '<div class="card"><span class="k">门诊病历</span><b><span class="tag orange">未写</span></b></div>' in html
    assert '<div class="card"><span class="k">诊断</span><b><span class="tag orange">无</span></b></div>' in html
