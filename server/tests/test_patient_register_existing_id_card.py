"""网页建档撞上已有证件号：回执说清「没有新建、档案没按本次所填改」（P2-1244，第三十六批扫描 U3-3）。

`POST /api/patients` 走 `create_patient_idempotent`：证件号已有档案就原样返回那一份，请求体一概不写。修之前把
`created` 丢掉，命中照样 201、回执与新建一模一样，页面（core.js 建档表单）一律报「建档成功，电子健康卡号：…」——

- 村卫生室早年建的档没有出生日期，镇卫生院窗口再建档补填 1946-03-01 → 201，库里仍是空，老年人群审方规则照旧不触发；
- 证件号录错撞上别人的档案（「李秀英 / 女」撞上「王老汉 / 男」）→ 201，照样「建档成功」。

同一个幂等建档，HL7 / FHIR 入站回执早就带 `created`。修法：回执只加 `created`（新建 true、命中 false），状态码照旧
201（对接方按 201 判成功，向后兼容）；页面据此提示「该证件号已建档，未按本次所填改动档案：……与档案不一致的项……，
如需更正请走档案更正」。本次值不替人写进档案——「档案为空时用本次值补上」要业务拍板，不在本条。
"""
import os

import pytest

from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: `PatientOut` 七键的原有次序（`test_patients_contract.PATIENT_KEYS`），`created` 只追加在末尾
PATIENT_KEYS = ["name", "id_card", "gender", "birth_date", "phone", "id", "ehc_no"]


@pytest.fixture(scope="module")
def operators(client, admin):
    heads = []
    for name, level in (("P21244 村卫生室", "village"), ("P21244 镇卫生院", "township")):
        org = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": level, "level": level}).json()["id"]
        username = f"p21244_op_{level}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "operator", "org_id": org})
        assert resp.status_code == 201, resp.text
        heads.append(login(client, username, "passw0rd1"))
    return heads


def test_新建照旧201_回执带created_true(client, operators):
    village, _ = operators
    resp = client.post("/api/patients", headers=village, json={
        "name": "新建居民", "id_card": "320981199001011232", "gender": "女", "birth_date": "1990-01-01"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert list(body) == PATIENT_KEYS + ["created"]                  # 只加字段，原七键次序不动
    assert body["created"] is True                                   # 修前没有这个键
    assert (body["name"], body["birth_date"]) == ("新建居民", "1990-01-01")


def test_证件号已建档_回执created_false_档案不按本次所填改动(client, admin, operators):
    village, township = operators
    # 村卫生室早年建过档：没有出生日期
    first = client.post("/api/patients", headers=village, json={
        "name": "王老汉", "id_card": "320981194603011238", "gender": "男", "phone": "13800001234"})
    assert first.status_code == 201 and first.json()["created"] is True, first.text

    # 镇卫生院窗口给同一个人再建档，补填出生日期、换了电话；再拿同一个证件号建「李秀英 / 女」（证件号录错撞上别人）
    for name, gender, birth in (("王老汉", "男", "1946-03-01"), ("李秀英", "女", "1950-05-05")):
        resp = client.post("/api/patients", headers=township, json={
            "name": name, "id_card": "320981194603011238", "gender": gender,
            "birth_date": birth, "phone": "13912340000"})
        assert resp.status_code == 201, resp.text                  # 状态码不改：对接方按 201 判成功
        body = resp.json()
        assert body["created"] is False, body                      # 修前回执与新建一模一样
        assert body["id"] == first.json()["id"] and body["ehc_no"] == first.json()["ehc_no"]
        # 回的是既有档案（照旧按角色脱敏，H1）：本次所填一概没写进去
        assert (body["name"], body["gender"], body["birth_date"], body["phone"]) == ("王老汉", "男", "", "138******34")

    # 库里档案原样：不替人把本次值写进档案（「档案为空时补上」要业务拍板）
    stored = client.get(f"/api/patients/{first.json()['ehc_no']}", headers=admin).json()
    assert (stored["name"], stored["gender"], stored["birth_date"], stored["phone"]) == ("王老汉", "男", "", "13800001234")
    assert len(client.get("/api/patients?keyword=320981194603011238", headers=admin).json()) == 1


def test_建档页按created区分提示_命中不报建档成功():
    """页面据 `created` 提示：命中时列出与档案不一致的项、指向档案更正，不再报「建档成功」（修前一律「建档成功」）。"""
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    handler = source[source.index('$("#patient-form").onsubmit'):]
    handler = handler[:handler.index('$("#patient-search").onsubmit')]
    hit = handler.index("if (p.created === false) {")
    otherwise = handler.index("} else {", hit)
    assert "该证件号已建档" in handler[hit:otherwise] and "如需更正请走档案更正" in handler[hit:otherwise]
    assert "registerConflicts(f, p)" in handler[hit:otherwise]
    assert handler.count("`建档成功，") == 1 and handler.index("`建档成功，") > otherwise   # 「建档成功」只在新建那一支

    helper = source[source.index("function registerConflicts(f, p) {"):]
    helper = helper[:helper.index("\n}\n")]
    for field, label in (("name", "姓名"), ("gender", "性别"), ("birth_date", "出生日期"), ("phone", "电话")):
        assert f'check("{field}", "{label}"' in helper, field
