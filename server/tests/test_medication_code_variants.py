"""药事监测的供应风险、用药画像、用药地图按编码的比对键认同一味药：换个写法（全角、小写、尾随空格）照样并成一条
（P2-1773，第五十二批扫描 AP4-1）。

缺药登记、库存与开方的药品编码都是手输框，提交前不 trim、不转半角（P2-785），小写、尾随空格、全角都原样落库；三处原先按原样
编码分组。2026-10-09 开发库实测：甲院甘精胰岛素 `A10AE04` 可发 0、阈值 20，乙院药师用中文输入法登记全角 `Ａ１０ＡＥ０４`，
供应风险返回两条「中」（`{A10AE04, 库存告警 1, 登记 0}`、`{Ａ１０ＡＥ０４, 0, 1}`），漏报「高」；`j01ca04`、`J01CA04 ` 的登记
各自另成一条「中」。患者在用 3 种药、续方编码写成 `c09aa02`、`C08CA05 `，画像 `distinct_drugs 5, in_use_drugs 5,
polypharmacy_warning True`（误报多重用药），用药地图里依那普利拆成两行、各 1 方。

修法：三处都以 `texttypes.code_key` 作归并键（与审方 P1-218、传染病多点预警 P2-1146 同一个比对键），落库的编码照原样；供应风险
同一家机构几种写法只算 1 家；用药地图先按原样编码分组计数、再按比对键并组（几种写法的组回表跨写法去重），排序与前 50 名的
上限挪到并组之后；显示的编码与药名取组内字典序最小的写法（有库存告警的药名照旧取库存行的名字，P2-1664）。
"""
import pytest

from app.database import SessionLocal
from app.texttypes import code_key


def _fullwidth(code: str) -> str:
    """半角可见字符转全角（中文输入法全角状态下敲出来的样子）。"""
    return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in code)


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P1773 卫生院{i}", "org_type": "township", "level": "township"}).json()["id"] for i in (1, 2, 3)]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P1773 患者{i}", "id_card": f"33010619700101177{i}"}).json()["id"] for i in (1, 2, 3)]
    return {"orgs": orgs, "patients": patients}


def _stock(client, admin, org, code, name):
    resp = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": name, "quantity": 0, "threshold": 20})
    assert resp.status_code == 200, resp.text


def _shortage(client, admin, org, code, name):
    resp = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": name, "quantity": 5})
    assert resp.status_code == 201, resp.text
    assert resp.json()["drug_code"] == code   # 落库的编码照原样


def _risks(client, admin, key):
    resp = client.get("/api/medication/supply-risk", headers=admin)
    assert resp.status_code == 200, resp.text
    return [r for r in resp.json()["risks"] if code_key(r["drug_code"]) == key]


# ---------------------------------------------------------------- 供应风险


@pytest.mark.parametrize("code, variant", [
    ("P1773-FW", _fullwidth), ("P1773-LC", str.lower), ("P1773-TS", lambda code: code + " "),
], ids=["全角", "小写", "尾随空格"])
def test_供应风险_写法不同的缺药登记与库存告警合成一条高风险(client, admin, world, code, variant):
    a, b, _ = world["orgs"]
    _stock(client, admin, a, code, "甘精胰岛素（库存）")
    _shortage(client, admin, b, variant(code), "甘精胰岛素（登记）")
    # 修前两条「中」：{原样编码, 库存告警 1, 登记 0}、{另一种写法, 0, 1}
    assert _risks(client, admin, code) == [{
        "drug_code": code, "drug_name": "甘精胰岛素（库存）", "low_stock_orgs": 1, "open_shortages": 1, "risk_level": "high"}]


def test_供应风险_同一家机构几种写法只算一家(client, admin, world):
    a, b, c = world["orgs"]
    _stock(client, admin, a, "P1773-MULTI", "阿莫西林胶囊")
    _stock(client, admin, a, "p1773-multi", "阿莫西林")       # 同一家，另一种写法又建了一行库存
    _stock(client, admin, c, _fullwidth("P1773-MULTI"), "阿莫西林胶囊（全角）")
    _shortage(client, admin, b, "P1773-MULTI ", "阿莫西林")
    # 修前四条「中」：三种写法的库存告警各 1 家、尾随空格的登记 1 条，没有「高」
    assert _risks(client, admin, "P1773-MULTI") == [{
        "drug_code": "P1773-MULTI", "drug_name": "阿莫西林胶囊", "low_stock_orgs": 2, "open_shortages": 1,
        "risk_level": "high"}]


def test_供应风险_只有登记的几种写法_登记条数相加_药名取字典序最小(client, admin, world):
    a, b, _ = world["orgs"]
    _shortage(client, admin, a, "P1773-ONLY", "二甲双胍缓释片")
    _shortage(client, admin, b, "p1773-only", "二甲双胍片")
    _shortage(client, admin, b, _fullwidth("P1773-ONLY"), "二甲双胍")
    assert _risks(client, admin, "P1773-ONLY") == [{
        "drug_code": "P1773-ONLY", "drug_name": "二甲双胍", "low_stock_orgs": 0, "open_shortages": 3,
        "risk_level": "medium"}]   # 修前三条，各 1 条登记


# ---------------------------------------------------------------- 用药画像 + 用药地图


def _prescribe(client, admin, world, code, name, dose=10):
    resp = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": world["patients"][0], "org_id": world["orgs"][0], "diagnosis_name": "高血压",
        "items": [{"drug_code": code, "drug_name": name, "daily_dose": dose, "days": 30}]})
    assert resp.status_code == 201 and resp.json()["status"] == "auto_passed", resp.text


def test_同一味药两种写法开两张方_画像算一种在用_次数2_不误报多重用药_地图一行两方(client, admin, world):
    _prescribe(client, admin, world, "P1773-ENA", "依那普利片", dose=10)
    _prescribe(client, admin, world, _fullwidth("p1773-ena"), "依那普利", dose=20)   # 续方换了写法
    for code in ("P1773-B", "P1773-C", "P1773-D"):
        _prescribe(client, admin, world, code, f"{code} 药")
    resp = client.get(f"/api/medication/profile/{world['patients'][0]}", headers=admin)
    assert resp.status_code == 200, resp.text
    profile = resp.json()
    # 修前 5 种在用、polypharmacy_warning True：依那普利两种写法算成两种药
    assert (profile["distinct_drugs"], profile["in_use_drugs"], profile["polypharmacy_warning"]) == (4, 4, False)
    ena = [d for d in profile["drugs"] if code_key(d["drug_code"]) == "P1773-ENA"]
    assert ena == [{"drug_code": "P1773-ENA", "drug_name": "依那普利", "times": 2, "max_daily_dose": 20.0, "in_use": True}]
    rows = [r for r in client.get("/api/medication/usage-stats", headers=admin).json()
            if code_key(r["drug_code"]) == "P1773-ENA"]
    assert rows == [{"drug_code": "P1773-ENA", "drug_name": "依那普利", "rx_count": 2, "patient_count": 1}]   # 修前两行各 1 方


def _approved(db, patient, org, lines):
    """直接落一张审方通过的处方（同一张方里两种写法并存，经接口会转药师审）。"""
    from app.models import Prescription, PrescriptionItem, User

    user = db.query(User).filter(User.username == "admin").one().id
    rx = Prescription(patient_id=patient, org_id=org, status="approved", created_by=user)
    db.add(rx)
    db.flush()
    for code, name in lines:
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code=code, drug_name=name, daily_dose=1.0, days=30))


def test_用药地图_几种写法跨写法数方数与人数_不按写法相加(client, admin, world):
    _, first, second = world["patients"]
    with SessionLocal() as db:
        _approved(db, first, world["orgs"][0], [("P1773-MET", "二甲双胍片"), ("p1773-met ", "二甲双胍缓释片")])
        _approved(db, second, world["orgs"][0], [(_fullwidth("P1773-MET"), "二甲双胍")])
        db.commit()
    rows = [r for r in client.get("/api/medication/usage-stats", headers=admin).json()
            if code_key(r["drug_code"]) == "P1773-MET"]
    # 修前三行各 1 方 1 人；按写法各数再相加则是 3 方 3 人——第一张方里两种写法各一行，只算一张方、一个人
    assert rows == [{"drug_code": "P1773-MET", "drug_name": "二甲双胍", "rx_count": 2, "patient_count": 2}]


def test_用药地图_并组之后再排序_取前50(client, admin, world):
    """排序（方数降序、同数按编码）与前 50 名的上限照旧，挪到并组之后：三种写法各 1 方的药并组后 3 方，与 50 味各 3 方的药
    并列，编码最小排第一；挤出去的是同为 3 方、编码最大的那一味。本模块其余用例的药都不到 3 方。"""
    patient, org = world["patients"][1], world["orgs"][0]
    with SessionLocal() as db:
        for i in range(50):
            for _ in range(3):
                _approved(db, patient, org, [(f"P1773-Z{i:02d}", f"填充药{i:02d}")])
        for spelling in ("P1773-Y", "p1773-y", "P1773-Y "):
            _approved(db, patient, org, [(spelling, "依折麦布")])
        db.commit()
    rows = client.get("/api/medication/usage-stats", headers=admin).json()
    assert len(rows) == 50
    # 修前三种写法各 1 方、挤不进前 50，前 50 是 Z00～Z49
    assert rows[0] == {"drug_code": "P1773-Y", "drug_name": "依折麦布", "rx_count": 3, "patient_count": 1}
    assert [r["drug_code"] for r in rows[1:]] == [f"P1773-Z{i:02d}" for i in range(49)]
    assert all(r["rx_count"] == 3 for r in rows)
