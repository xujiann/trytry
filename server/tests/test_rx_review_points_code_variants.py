"""处方点评要点按开方时认规则的同一个找法取说明文字：编码写法不同的处方照样带出点评要点与肝肾提示（P2-1660，第四十九批
「集中审方与药事监测」扫描 AM3-1）。

`prescription_review_points` 有快照的分支原先用 `DrugRule.drug_code == item.drug_code` 原样找规则行取说明文字。实测（修前）：
华法林编码写成 `b01aa03`、`B01AA03 ` 的处方，系统审照规则（P1-218 按比对键认）转了药师审、开方回执带着肝肾提示、明细记下了
快照，点评接口却按原样编码找不到规则——`review_points`、`renal_hepatic_note` 都是空串而 `no_rule=False`、覆盖率 100%，
页面印「要点：规则库未维护／肝肾：—」，点评药师看不到 INR、出血风险这些要点。

修法：原样找不到时再按 `texttypes.code_key` 找一遍（与 `_active_rule` 同一个找法、撞上多条取 id 最小的），且不滤生效位——
停用的规则行也要能回溯说明文字。
"""
import pytest

from conftest import login

CODE = "P1660W01"
RETIRED = "P1660R01"
POINTS = "P1660 是否监测 INR 并记录目标范围；是否评估出血风险"
NOTE = "P1660 肝功能不全者出血风险显著升高"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1660 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, role in (("p1660_doc", "doctor"), ("p1660_ph", "pharmacist")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1660 患者", "id_card": "330102195001011660"}).json()["id"]
    for code in (CODE, RETIRED):
        created = client.post("/api/prescriptions/rules", headers=admin, json={
            "drug_code": code, "max_daily_dose": 10, "dose_unit": "mg", "review_points": POINTS,
            "renal_hepatic_note": NOTE})
        assert created.status_code == 201, created.text
    return {"org": org, "patient": patient, "doctor": login(client, "p1660_doc", "passw0rd1"),
            "pharmacist": login(client, "p1660_ph", "passw0rd1")}


def _rx(client, world, code: str) -> int:
    resp = client.post("/api/prescriptions", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": "心房颤动",
        "items": [{"drug_code": code, "drug_name": "华法林", "daily_dose": 15, "days": 7}]})
    assert resp.status_code == 201, resp.text
    # 系统审按比对键认到了规则：超量转药师审、回执带肝肾提示（快照有值）
    assert resp.json()["status"] == "pending_review", resp.json()
    return resp.json()["id"]


def _item(client, world, rx_id: int) -> dict:
    resp = client.get(f"/api/prescriptions/{rx_id}/review-points", headers=world["pharmacist"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule_coverage_pct"] == 100.0, body
    return body["items"][0]


@pytest.mark.parametrize("variant", [CODE.lower(), "Ｐ１６６０Ｗ０１", CODE + " "], ids=["小写", "全角", "尾随空格"])
def test_写法不同的编码_点评要点与肝肾提示与原样编码逐字相同(client, world, variant):
    expected = _item(client, world, _rx(client, world, CODE))
    assert (expected["review_points"], expected["renal_hepatic_note"]) == (POINTS, NOTE)   # 原样命中的照旧
    got = _item(client, world, _rx(client, world, variant))
    # 修前两个都是空串（no_rule=False、覆盖率 100%，页面印「规则库未维护」）
    assert (got["review_points"], got["renal_hepatic_note"]) == (expected["review_points"], expected["renal_hepatic_note"])
    assert (got["max_daily_dose"], got["dose_unit"], got["dose_exceeded"], got["no_rule"]) == (10.0, "mg", True, False)


def test_规则停用后_写法不同的编码照样回溯得到说明文字(client, admin, world):
    rx_id = _rx(client, world, RETIRED.lower())
    off = client.delete(f"/api/prescriptions/rules/{RETIRED}", headers=admin)
    assert off.status_code == 200, off.text
    item = _item(client, world, rx_id)
    # 不滤生效位：停用的规则行也要能回溯（快照上的上限照旧按开方时判读）
    assert (item["review_points"], item["renal_hepatic_note"]) == (POINTS, NOTE)
    assert (item["max_daily_dose"], item["no_rule"]) == (10.0, False)


def test_比对键撞上停用旧行与生效行_取生效的那条说明文字(client, admin, world):
    """先建的 `P1660D01` 停用了、后建的 `p1660d01` 生效（建规则按原样编码去重，两行并存，P2-785）：开方写全角编码，
    系统审按比对键认的是生效那条；点评说明文字也要取它，不能被 id 更小的停用旧行顶替。"""
    for code, points in (("P1660D01", "P1660 旧版要点"), ("p1660d01", "P1660 新版要点")):
        created = client.post("/api/prescriptions/rules", headers=admin, json={
            "drug_code": code, "max_daily_dose": 10, "dose_unit": "mg", "review_points": points,
            "renal_hepatic_note": NOTE})
        assert created.status_code == 201, created.text
        if code == "P1660D01":
            assert client.delete(f"/api/prescriptions/rules/{code}", headers=admin).status_code == 200
    item = _item(client, world, _rx(client, world, "Ｐ１６６０Ｄ０１"))
    assert item["review_points"] == "P1660 新版要点"

