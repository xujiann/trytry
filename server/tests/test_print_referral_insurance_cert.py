"""签了医保转诊证明的转诊单，打印件印证明号、签发时间与签发人（P2-1243，第三十六批「打印件与对外文书的必备要素」
扫描 U4-6 的 clear 部分）。

医保转诊证明（`referral_certs`）签出来只在回执里闪一行证明号：没有打印、没有查询，全仓库除了签发本身没有地方读它；
窗口能核对的只有转诊单，而同一转诊打出的转诊单上没有证明号（修前实测：签发回执 `ZZ9CEFE92B8B`，转诊单只有
「单据编号：ZZ00000001」，纸面含证明号 False）。证明是凭证，出了争议要答得上谁签的（P2-524 起落了签发人）。

修法：已签证明的转诊单打印件加一节「医保转诊证明」（证明号 / 签发时间 / 签发人；签发人没落库的存量写「—」）；
没签的转诊单字节不变。转诊单自己的编号前缀不改（换前缀改的是已发出纸面的编号口径，待裁定），证明要不要单独出纸、
要不要查询入口也待业务定，都不在本条。
"""
from datetime import datetime

import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import Referral, ReferralCert
from conftest import login

COUNTY, TOWN = "P21243 县人民医院", "P21243 甲乡卫生院"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": COUNTY, "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": TOWN, "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    for username, role, org_id, full_name in (("p21243_town", "doctor", town, "乡医张三"),
                                              ("p21243_county", "doctor", county, "接诊李四"),
                                              ("p21243_op", "operator", county, "医保经办吴二")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org_id, "full_name": full_name})
        assert created.status_code in (200, 201), created.text
    town_doc, county_doc, operator = (login(client, u, "passw0rd1") for u in ("p21243_town", "p21243_county", "p21243_op"))
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21243 患者", "id_card": "330281196001011243", "gender": "女", "birth_date": "1960-01-01",
        "phone": "13800001243"}).json()

    def refer(reason, accept=True):
        resp = client.post("/api/referrals", headers=town_doc, json={
            "patient_id": patient["id"], "from_org_id": town, "to_org_id": county, "direction": "up",
            "reason": reason})
        assert resp.status_code == 201, resp.text
        if accept:
            moved = client.patch(f"/api/referrals/{resp.json()['id']}/status", headers=county_doc,
                                 json={"status": "accepted"})
            assert moved.status_code == 200, moved.text
        return resp.json()["id"]

    signed = refer("肺炎加重，请上级收治")
    cert = client.post(f"/api/insurance/referral-certs/{signed}?patient_id={patient['id']}", headers=operator)
    assert cert.status_code == 200, cert.text
    unsigned = refer("心悸待查，请上级会诊", accept=False)
    legacy = refer("骨折复位后转回", accept=True)
    with SessionLocal() as db:   # 签发人落库（P2-524）之前签的存量证明：issued_by 为空
        db.add(ReferralCert(referral_id=legacy, cert_no="ZZLEGACY1243", issued_at=datetime(2026, 9, 26, 23, 30)))
        db.commit()
    return {"county_doc": county_doc, "patient": patient, "signed": signed, "cert_no": cert.json()["cert_no"],
            "unsigned": unsigned, "legacy": legacy}


def _print(client, headers, referral_id):
    resp = client.get(f"/api/print/referrals/{referral_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def _cert_rows(html: str) -> list[str]:
    section = html[html.index("<h3>医保转诊证明</h3>"):html.index('<div class="sign">')]
    return [section[section.index("<thead>"):section.index("</thead>")],
            section[section.index("<tbody>"):section.index("</tbody>")]]


def test_签了证明的转诊单印证明号签发时间与签发人(client, world):
    html = _print(client, world["county_doc"], world["signed"])
    with SessionLocal() as db:
        cert = db.query(ReferralCert).filter(ReferralCert.referral_id == world["signed"]).one()
        issued = to_local(cert.issued_at).strftime("%Y-%m-%d %H:%M")
    # 修前纸面上没有证明号，只有转诊单自己的「单据编号：ZZ00000001」
    assert _cert_rows(html) == [
        "<thead><tr><th>医保转诊证明号</th><th>签发时间</th><th>签发人</th></tr>",
        f"<tbody><tr><td>{world['cert_no']}</td><td>{issued}</td><td>医保经办吴二</td></tr>",
    ]
    assert f"单据编号：ZZ{world['signed']:08d}" in html          # 转诊单编号口径不动（换前缀待裁定）


def test_签发人没落库的存量证明签发人写横杠(client, world):
    html = _print(client, world["county_doc"], world["legacy"])
    issued = to_local(datetime(2026, 9, 26, 23, 30)).strftime("%Y-%m-%d %H:%M")
    assert _cert_rows(html)[1] == f"<tbody><tr><td>ZZLEGACY1243</td><td>{issued}</td><td>—</td></tr>"


def test_没签证明的转诊单字节不变(client, world):
    html = _print(client, world["county_doc"], world["unsigned"])
    p = world["patient"]
    with SessionLocal() as db:
        applied = to_local(db.get(Referral, world["unsigned"]).created_at).strftime("%Y-%m-%d %H:%M")
    sheet = html[html.index('<table class="meta">'):html.index('<div class="qr">')]
    assert sheet == (
        '<table class="meta">'
        '<tr><td class="k">姓名</td><td>P21243 患者</td><td class="k">性别</td><td>女</td></tr>'
        f'<tr><td class="k">出生日期</td><td>1960-01-01</td><td class="k">健康卡号</td><td>{p["ehc_no"]}</td></tr>'
        '<tr><td class="k">身份证号</td><td>3302**********1243</td><td class="k">联系电话</td><td>138******43</td></tr>'
        f'<tr><td class="k">转出机构</td><td>{TOWN}</td><td class="k">转入机构</td><td>{COUNTY}</td></tr>'
        '<tr><td class="k">转诊方向</td><td>上转</td><td class="k">当前状态</td><td>待接诊</td></tr>'
        f'<tr><td class="k">申请时间</td><td colspan="3">{applied}</td></tr></table>\n'
        '  \n'
        '  <div class="section"><h3>转诊事由</h3><div class="body">心悸待查，请上级会诊</div></div>\n'
        '  <div class="sign"><span>申请医师：乡医张三</span>\n'
        '    <span>接诊签收：____________</span></div>\n'
        '  '
    )
