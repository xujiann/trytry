"""出院小结署写出院记录的医师与记录时间（P2-1240，第三十六批「打印件与对外文书的必备要素」扫描 U4-4）。

出院小结原先 meta 与签名行都只取入院登记时选填的「主管医师」（`admission.doctor_name`，页面缺省为空）；出院病程取了，
却只用它的正文。入院时主管医师留空、出院记录由「住院医周一」书写，打印件上是「主管医师 —」「主管医师：—」，整张纸
没有一个医师名（修前实测：纸面含『住院医周一』False）。同一批住院单据里，病案首页署了「填写医师 / 填写时间」。

修法：签名行加署出院病程的记录医师与记录时间（病案首页署填写医师 / 填写时间的同一个位置），主管医师那一栏照旧留着
（meta 里也还有）；没写出院病程的两栏写「—」；记录时间没填的存量病程按落库时刻换本地时间再印（与病程清单
`_shown_time` 同一个取法）。同一次住院的病案首页字节不变。
"""
from datetime import datetime

import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import Admission, CaseSummary, ProgressNote, User
from conftest import login

ORG = "P21240 县人民医院"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": ORG, "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21240_ward", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "住院医周一"})
    assert created.status_code in (200, 201), created.text
    doc = login(client, "p21240_ward", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21240 内科"}).json()

    def admit(n, doctor_name=""):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21240 患者{n}", "id_card": f"33028119550707{1240 + n}", "gender": "男",
            "birth_date": "1955-07-07"}).json()
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": f"P{n}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=doc, json={
            "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "doctor_name": doctor_name,
            "diagnosis_name": "社区获得性肺炎"})
        assert adm.status_code == 201, adm.text
        return patient, adm.json()["id"]

    def note(admission_id, **fields):
        resp = client.post(f"/api/inpatient/admissions/{admission_id}/progress-notes", headers=doc, json={
            "note_type": "discharge", "content": "抗感染7天，体温正常3日，复查胸片吸收，予出院。", **fields})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def discharge(admission_id):   # 出院前须填病案首页
        summary = client.post(f"/api/inpatient/admissions/{admission_id}/case-summary", headers=doc, json={
            "discharge_diagnosis": "社区获得性肺炎（治愈）", "operation": "", "total_cost": 0, "drug_cost": 0,
            "outcome": "治愈"})
        assert summary.status_code == 201, summary.text
        assert client.post(f"/api/inpatient/admissions/{admission_id}/discharge", headers=doc,
                           json={}).status_code == 200

    patient, unsigned = admit(0)                                   # 主管医师没填（页面选填）
    written = note(unsigned)
    discharge(unsigned)

    _, signed = admit(1, doctor_name="钱医师")                      # 主管医师填了，出院记录另有人写、补记了时间
    note(signed, doctor_name="李医师", recorded_at="2026-09-30 10:00")
    discharge(signed)

    _, bare = admit(2)                                             # 没写出院病程就出院
    discharge(bare)

    _, legacy = admit(3)                                           # 记录时间没填的存量病程（P2-455 之前落库的）
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "p21240_ward").scalar()
        db.add(ProgressNote(admission_id=legacy, note_type="discharge", content="存量出院记录", doctor_name="住院医周一",
                            recorded_at="", created_by=creator, created_at=datetime(2026, 9, 26, 23, 30)))
        db.commit()
    discharge(legacy)
    return {"doc": doc, "patient": patient, "unsigned": unsigned, "written": written, "signed": signed,
            "bare": bare, "legacy": legacy}


def _print(client, headers, path):
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def _sign(recorder: str, recorded: str, attending: str = "—") -> str:
    return (f'<div class="sign"><span>记录医师：{recorder}</span>\n'
            f'    <span>记录时间：{recorded}</span>\n'
            f'    <span>主管医师：{attending}</span>\n'
            '    <span>打印核对：____________</span></div>')


def test_主管医师没填时署出院病程的作者与时间(client, world):
    html = _print(client, world["doc"], f"/api/print/discharge-summaries/{world['unsigned']}")
    assert world["written"]["doctor_name"] == "住院医周一"
    assert _sign("住院医周一", world["written"]["recorded_at"]) in html      # 修前整张纸没有「住院医周一」
    assert '<td class="k">主管医师</td><td>—</td>' in html                   # 主管医师照旧留在 meta


def test_署的是出院病程的记录医师与补记的记录时间_主管医师照旧在(client, world):
    html = _print(client, world["doc"], f"/api/print/discharge-summaries/{world['signed']}")
    assert _sign("李医师", "2026-09-30 10:00", "钱医师") in html             # 主管医师那一栏照旧在签名行
    assert '<td class="k">主管医师</td><td>钱医师</td>' in html


def test_没写出院病程的两栏写横杠(client, world):
    html = _print(client, world["doc"], f"/api/print/discharge-summaries/{world['bare']}")
    assert _sign("—", "—") in html
    assert '<h3>诊疗经过（出院病程记录）</h3><div class="body">—</div>' in html


def test_记录时间没填的存量病程按落库时刻换本地时间(client, world):
    html = _print(client, world["doc"], f"/api/print/discharge-summaries/{world['legacy']}")
    assert _sign("住院医周一", to_local(datetime(2026, 9, 26, 23, 30)).strftime("%Y-%m-%d %H:%M")) in html


def test_同一次住院的病案首页字节不变(client, world):
    html = _print(client, world["doc"], f"/api/print/case-summaries/{world['unsigned']}")
    p = world["patient"]
    with SessionLocal() as db:
        adm = db.get(Admission, world["unsigned"])
        summary = db.query(CaseSummary).filter(CaseSummary.admission_id == adm.id).one()
        admitted, discharged, filled = (to_local(t).strftime("%Y-%m-%d %H:%M")
                                        for t in (adm.admitted_at, adm.discharged_at, summary.created_at))
        drg = f"{summary.drg_code}（权重 {summary.drg_weight}）" if summary.drg_code else "未入组"
    sheet = html[html.index('<table class="meta">'):html.index('<div class="qr">')]
    assert sheet == (
        '<table class="meta">'
        '<tr><td class="k">姓名</td><td>P21240 患者0</td><td class="k">性别</td><td>男</td></tr>'
        f'<tr><td class="k">出生日期</td><td>1955-07-07</td><td class="k">健康卡号</td><td>{p["ehc_no"]}</td></tr>'
        '<tr><td class="k">身份证号</td><td>3302**********1240</td><td class="k">联系电话</td><td>—</td></tr>'
        f'<tr><td class="k">住院机构</td><td>{ORG}</td><td class="k">主管医师</td><td>—</td></tr>'
        f'<tr><td class="k">入院时间</td><td>{admitted}</td><td class="k">出院时间</td><td>{discharged}</td></tr>'
        f'<tr><td class="k">入院诊断</td><td>社区获得性肺炎</td><td class="k">DRG 分组</td><td>{drg}</td></tr></table>\n'
        '  \n'
        '  <div class="section"><h3>出院诊断</h3><div class="body">社区获得性肺炎（治愈）</div></div>\n'
        '  <div class="section"><h3>手术及操作</h3><div class="body">—</div></div>\n'
        '  <div class="section"><h3>费用与转归</h3>\n'
        '    <table class="items"><thead><tr><th>总费用(元)</th><th>其中药费(元)</th><th>转归</th></tr></thead>\n'
        '    <tbody><tr><td>0.00</td><td>0.00</td>\n'
        '      <td>治愈</td></tr></tbody></table></div>\n'
        '  <div class="section"><h3>备注</h3><div class="body">—</div></div>\n'
        '  <div class="sign"><span>填写医师：住院医周一</span>\n'
        f'    <span>填写时间：{filled}</span></div>\n'
        '  '
    )
