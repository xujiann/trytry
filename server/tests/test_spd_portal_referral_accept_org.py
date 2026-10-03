"""居民端慢专病转诊的「转入」在下转之前也取实际接收的那家县医院（P2-1191，第三十四批「跨机构协作的两端」扫描 L2-4）。

`referral_ends` 原先只有下转过的单子才取轨迹里「县级医院接收·通过」那一步的机构（P2-558 只修了下转之后），没下转的一律取
`target_org_id`——那只是发起时填的目标：手机端写着「可空：不写目标的由审核环节定」，留空的单子县医院接收、到院之后卡片
仍是「已到院就诊，转入＝空」；接收权按机构树、不按目标（L2-2），目标填中医院、由人民医院接收的，卡片先写「转入＝中医院」
（没接收的那家），一下转又变成人民医院。修前实测（scan34 l2/r5、r1）：`current_org_id=1 target_org_id=None` 的卡片
`转入=''`；接收后「转入=县中医院X」，下转后「转入=县人民医院X」。函数自己的 docstring 写的就是「上转去的是哪家县医院只剩
轨迹里有：县级医院接收那一步的机构」。

修法：轨迹里有「县级医院接收·通过」就取那一步的机构，不论下转过没有；没有才退回 `target_org_id`。居民端清单与详情同源
（都经 `referral_ends`），一处改。
"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import SmsCode
from conftest import login

B = "/api/spd"
PHONE = "13913311910"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs: dict[str, int] = {}
    for key, name, level, org_type, parent in (
        ("people", "P1191 县人民医院", "county", "lead_hospital", None),
        ("tcm", "P1191 县中医院", "county", "lead_hospital", None),   # 另一棵树的根：不是卫生院的上级
        ("town", "P1191 卫生院", "township", "township", "people"),
        ("village", "P1191 村卫生室", "village", "village", "town"),
    ):
        body = {"name": name, "level": level, "org_type": org_type}
        if parent:
            body["parent_id"] = orgs[parent]
        resp = client.post("/api/organizations", headers=admin, json=body)
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key in ("people", "town", "village"):
        username = f"p1191_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor",
            "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1191 居民", "id_card": "330127195804041191", "phone": PHONE}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": orgs["village"]})
    assert enrolled.status_code == 201, enrolled.text
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"orgs": orgs, "heads": heads, "patient": patient, "resident": {"Authorization": f"Bearer {token}"}}


def _step(client, world, who, case_id, path, body):
    resp = client.post(f"{B}/referrals/{case_id}/{path}", headers=world["heads"][who], json=body)
    assert resp.status_code == 200, (who, path, resp.text)
    return resp.json()


def _up_and_accept(client, world, target_org_id):
    """村医发起上转（目标机构可空）→ 卫生院审核 → 县人民医院（卫生院的上级）接收。"""
    body = {"patient_id": world["patient"], "program_code": "hypertension", "reason": "P1191 血压控制不佳"}
    if target_org_id is not None:
        body["target_org_id"] = target_org_id
    resp = client.post(f"{B}/referrals", headers=world["heads"]["village"], json=body)
    assert resp.status_code == 201, resp.text
    case_id = resp.json()["id"]
    _step(client, world, "town", case_id, "review", {"action": "pass"})
    accepted = _step(client, world, "people", case_id, "review", {"action": "pass"})
    assert (accepted["status"], accepted["target_org_id"]) == ("accepted", target_org_id)   # 接收不改写目标
    return case_id


def _ends(client, world, case_id):
    """居民端卡片与详情上的两端机构：卡片（转出, 转入），详情（转出, 转入, 下转至）。"""
    feed = client.get("/api/portal/me/referrals/all", headers=world["resident"], params={"source": "spd"})
    assert feed.status_code == 200, feed.text
    card = next(r for r in feed.json() if r["id"] == case_id)
    detail = client.get(card["detail_path"], headers=world["resident"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    return (card["from_org"], card["to_org"]), (body["from_org"], body["to_org"], body["down_to_org"])


def test_目标留空_接收到院后转入是接收的县医院(client, world):
    case_id = _up_and_accept(client, world, None)
    _step(client, world, "people", case_id, "arrive", {"effective_visit": True})
    card, detail = _ends(client, world, case_id)
    assert card == ("P1191 村卫生室", "P1191 县人民医院")   # 修前转入是空串
    assert detail == ("P1191 村卫生室", "P1191 县人民医院", "")


def test_目标填中医院_人民医院接收_下转前后转入都是人民医院(client, world):
    case_id = _up_and_accept(client, world, world["orgs"]["tcm"])
    card, detail = _ends(client, world, case_id)
    assert card == ("P1191 村卫生室", "P1191 县人民医院")   # 修前写没接收的县中医院
    assert detail == ("P1191 村卫生室", "P1191 县人民医院", "")
    _step(client, world, "people", case_id, "arrive", {"effective_visit": True})
    _step(client, world, "people", case_id, "down", {"target_org_id": world["orgs"]["town"]})
    card, detail = _ends(client, world, case_id)
    assert card == ("P1191 村卫生室", "P1191 县人民医院")   # 下转之后原先就对（P2-558）
    assert detail == ("P1191 村卫生室", "P1191 县人民医院", "P1191 卫生院")


def test_还没接收的照旧取目标机构(client, world):
    """没有「县级医院接收·通过」这一步（待卫生院审核、被县医院退回）：仍按发起时填的目标。"""
    resp = client.post(f"{B}/referrals", headers=world["heads"]["village"], json={
        "patient_id": world["patient"], "program_code": "hypertension", "reason": "P1191 待审",
        "target_org_id": world["orgs"]["tcm"]})
    assert resp.status_code == 201, resp.text
    pending = resp.json()["id"]
    assert _ends(client, world, pending)[0] == ("P1191 村卫生室", "P1191 县中医院")
    resp = client.post(f"{B}/referrals", headers=world["heads"]["village"], json={
        "patient_id": world["patient"], "program_code": "hypertension", "reason": "P1191 县级退回",
        "target_org_id": world["orgs"]["tcm"]})
    assert resp.status_code == 201, resp.text
    rejected = resp.json()["id"]
    _step(client, world, "town", rejected, "review", {"action": "pass"})
    _step(client, world, "people", rejected, "review", {"action": "reject", "opinion": "P1191 床位紧张"})
    assert _ends(client, world, rejected)[0] == ("P1191 村卫生室", "P1191 县中医院")
