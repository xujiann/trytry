"""出生 / 死亡医学证明的打印件按类型印「出生日期」/「死亡日期」（P2-1242，第三十六批「打印件与对外文书的必备要素」扫描
U4-9 的标签部分）。

证明只存一个 `event_date`，打印件原先一律印「事件日期」：死亡证明纸面是「事件日期 2026-09-28」，出生证明是「事件日期
2026-09-01」——家属拿去办户籍注销、殡葬、落户，读不出这是哪一天。签发校验（P2-940）与死因报告卡导出早就管它叫
「死亡日期」。

修法：出生医学证明印「出生日期」、死亡医学证明印「死亡日期」；出生缺陷儿登记的这个日期指出生还是诊断，库里没说，
照旧「事件日期」，字节不变。证明的其余字段集、签发人资格、标题要不要改都等业务拍板（U4-9 的其余部分），不在本条。
"""
import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import MedicalCert
from conftest import login

ORG = "P21242 县人民医院"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": ORG, "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21242_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "产科赵六"})
    assert created.status_code in (200, 201), created.text
    doc = login(client, "p21242_doc", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21242 逝者", "id_card": "330281194103051242", "gender": "男", "birth_date": "1941-03-05"}).json()

    def issue(cert_type, name, event_date, detail="", patient_id=None):
        resp = client.post("/api/certs", headers=doc, json={
            "cert_type": cert_type, "name": name, "gender": "男", "event_date": event_date, "detail": detail,
            "org_id": org, "patient_id": patient_id})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return {
        "doc": doc,
        "birth": issue("birth", "新生儿甲", "2026-09-01"),
        "death": issue("death", "P21242 逝者", "2026-09-28", detail="急性心肌梗死", patient_id=patient["id"]),
        "defect": issue("defect", "新生儿乙", "2026-09-03", detail="唇腭裂"),
    }


def _print(client, headers, cert_id):
    resp = client.get(f"/api/print/certs/{cert_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.parametrize("cert_type, label, day", [("birth", "出生日期", "2026-09-01"),
                                                   ("death", "死亡日期", "2026-09-28")])
def test_出生死亡证明按类型印日期的名称(client, world, cert_type, label, day):
    html = _print(client, world["doc"], world[cert_type]["id"])
    # 修前一律印「事件日期」
    assert f'<td class="k">{label}</td><td>{day}</td><td class="k">证明类别</td>' in html
    assert "事件日期" not in html


def test_出生缺陷儿登记照旧印事件日期_字节不变(client, world):
    html = _print(client, world["doc"], world["defect"]["id"])
    with SessionLocal() as db:
        issued_at = to_local(db.get(MedicalCert, world["defect"]["id"]).created_at).strftime("%Y-%m-%d %H:%M")
    sheet = html[html.index('<table class="meta">'):html.index('<div class="qr">')]
    assert sheet == (
        '<table class="meta">'
        '<tr><td class="k">姓名</td><td>新生儿乙</td><td class="k">性别</td><td>男</td></tr>'
        '<tr><td class="k">事件日期</td><td>2026-09-03</td><td class="k">证明类别</td><td>出生缺陷儿登记</td></tr>'
        f'<tr><td class="k">签发机构</td><td colspan="3">{ORG}</td></tr></table>\n'
        '  \n'
        '  <div class="section"><h3>诊断/说明</h3><div class="body">唇腭裂</div></div>\n'
        '  <div class="sign"><span>签发人：产科赵六</span>\n'
        f'    <span>签发时间：{issued_at}</span>\n'
        '    <span>签发机构（章）：____________</span></div>\n'
        '  '
    )
