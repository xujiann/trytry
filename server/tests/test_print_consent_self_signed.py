"""居民端本人自签的同意书，打印件印签署账户的凭据（P2-1241，第三十六批「打印件与对外文书的必备要素」扫描 U4-10）。

自签（`method=self`）记 `resident_account_id`、佐证为空——`portal.py` 的注释原话：「自签不需要 evidence……记录落
resident_account_id 即可回溯到账户」。打印件却不印这个账户：采集方式「居民端本人自签」、佐证材料「—」、签署人一栏
空线「____________」、经办人「—」，整张纸没有任何签署凭据（修前实测）。

修法：自签的佐证栏印「居民端账户 #id（手机号掩码）电子确认」，手机号一律 `mask_phone`（管理员打印也掩：账户手机号是
登录凭据，纸面只需认得出是哪个账户；只用微信、没绑手机号的不带括号），签名行签署人一栏写「电子签署」。窗口代录的
字节不变。
"""
from html import escape

import pytest

from app.clock import to_local
from app.database import SessionLocal
from app.models import ConsentRecord, ConsentText, ResidentAccount, SmsCode
from conftest import login

ORG = "P21241 卫生院"
PHONE = "13900001241"


def _resident_login(client, phone: str) -> dict:
    with SessionLocal() as db:   # 同号的发码冷却（与 test_consents 的 `_clear_cooldown` 同一个做法）
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": ORG, "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21241_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "乡医张三"})
    assert created.status_code in (200, 201), created.text
    doc = login(client, "p21241_doc", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21241 居民", "id_card": "330281198001011241", "gender": "男", "birth_date": "1980-01-01",
        "phone": PHONE}).json()
    encounter = client.post("/api/encounters", headers=doc, json={    # 本院接诊过，医生才打得到这位居民的单据
        "patient_id": patient["id"], "org_id": org, "encounter_type": "outpatient", "diagnosis_name": "高血压"})
    assert encounter.status_code == 201, encounter.text
    resident = _resident_login(client, PHONE)                          # 手机号唯一命中，登录即实名绑定
    signed = client.post("/api/portal/me/consents", headers=resident, json={"scene": "followup"})
    assert signed.status_code == 201, signed.text
    assert (signed.json()["method"], signed.json()["evidence"]) == ("self", "")
    proxy = client.post("/api/consents", headers=doc, json={
        "patient_id": patient["id"], "scene": "archive", "evidence": "签字影像附件#1241"})
    assert proxy.status_code == 201, proxy.text
    with SessionLocal() as db:   # 只用微信登录、没绑手机号的账户（微信登录要走开放平台回调，库里直接落）
        account = ResidentAccount(wechat_openid="p21241-openid")
        db.add(account)
        db.flush()
        wechat = ConsentRecord(patient_id=patient["id"], scene="followup", text_version="v1", method="self",
                               resident_account_id=account.id)
        db.add(wechat)
        db.commit()
        wechat_ids = {"account": account.id, "record": wechat.id}
    return {"doc": doc, "patient": patient, "signed": signed.json(), "proxy": proxy.json()["id"],
            "wechat": wechat_ids}


def _print(client, headers, record_id):
    resp = client.get(f"/api/print/consents/{record_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def test_自签的打印件印签署账户与电子签署(client, world):
    html = _print(client, world["doc"], world["signed"]["id"])
    account = world["signed"]["resident_account_id"]
    # 修前佐证栏是「—」、签署人一栏是空线
    assert f'<td class="k">佐证材料</td><td>居民端账户 #{account}（139******41）电子确认</td>' in html
    assert ('<div class="sign"><span>签署人（患者/监护人）：电子签署</span>\n'
            '    <span>经办人：—</span></div>') in html
    assert PHONE not in html


def test_管理员打印也只印账户手机号的掩码(client, admin, world):
    html = _print(client, admin, world["signed"]["id"])
    account = world["signed"]["resident_account_id"]
    assert f"<td>居民端账户 #{account}（139******41）电子确认</td>" in html


def test_没绑手机号的账户不带括号(client, world):
    html = _print(client, world["doc"], world["wechat"]["record"])
    assert f'<td class="k">佐证材料</td><td>居民端账户 #{world["wechat"]["account"]} 电子确认</td>' in html
    assert "签署人（患者/监护人）：电子签署" in html


def test_窗口代录的字节不变(client, world):
    html = _print(client, world["doc"], world["proxy"])
    p = world["patient"]
    with SessionLocal() as db:
        record = db.get(ConsentRecord, world["proxy"])
        signed_at = to_local(record.created_at).strftime("%Y-%m-%d %H:%M")
        text = (db.query(ConsentText)
                .filter(ConsentText.scene == "archive", ConsentText.version == record.text_version).one())
    sheet = html[html.index('<table class="meta">'):html.index('<div class="qr">')]
    assert sheet == (
        '<table class="meta">'
        '<tr><td class="k">姓名</td><td>P21241 居民</td><td class="k">性别</td><td>男</td></tr>'
        f'<tr><td class="k">出生日期</td><td>1980-01-01</td><td class="k">健康卡号</td><td>{p["ehc_no"]}</td></tr>'
        '<tr><td class="k">身份证号</td><td>3302**********1241</td><td class="k">联系电话</td><td>139******41</td></tr>'
        '<tr><td class="k">同意场景</td><td>居民健康建档</td><td class="k">采集方式</td><td>窗口代录</td></tr>'
        f'<tr><td class="k">签署时间</td><td>{signed_at}</td><td class="k">佐证材料</td><td>签字影像附件#1241</td></tr>'
        '</table>\n'
        '  \n'
        f'  <div class="section"><h3>告知内容</h3><div class="body">{escape(text.content)}</div>'
        f'<p>（文本版本：{text.version}；记录引用版本：{record.text_version}）</p></div>\n'
        '  \n'
        '  <div class="sign"><span>签署人（患者/监护人）：____________</span>\n'
        '    <span>经办人：乡医张三</span></div>\n'
        '  '
    )
