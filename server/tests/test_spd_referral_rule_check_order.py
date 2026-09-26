"""转诊规则试算不排序、「命中即开单」取第一条：命中几条时开出的上转单去向由库的返回次序决定（P2-304）。

`check_referral_rules` 逐条求值 `query.all()`——没有 ORDER BY；勾了「命中即开上转单」时取 `hits[0]` 的名称、目标机构与
规则编码开单。规则清单（`GET /referral-rules`）按编号排，试算却不排。PG 不保证无 ORDER BY 的次序：改过的规则（UPDATE 把
新行版本写到堆尾）从此排到后面——改一下规则名称，同一个患者再试算，开出去的单子就换了去向。

修法：按规则编号排，与清单同一个次序。SQLite 无 ORDER BY 时按 rowid 返回、恰好就是编号序，行为用例在测试库上测不出
修前修后之分——它记下约定的次序；防回退靠下面的静态钉。
"""
import ast
import inspect
import textwrap

import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    county, other = (client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "lead_hospital", "level": "county"}).json()["id"] for name in ("P2304 甲院", "P2304 乙院"))
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2304 患者", "id_card": "330127197309092304"}).json()["id"]
    township = client.post("/api/organizations", headers=admin, json={
        "name": "P2304 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": township})   # 开单要推得出发起机构
    assert enrolled.status_code == 201, enrolled.text
    # 先建的本病种规则去甲院、后建的通用规则去乙院，两条都按「血压 >= 180」命中
    for code, program, target in (("P2304_HTN", "hypertension", county), ("P2304_ANY", "", other)):
        created = client.post(f"{B}/referral-rules", headers=admin, json={
            "code": code, "name": f"{code} 血压危急", "program_code": program, "target_org_id": target,
            "conditions": [{"field": "bp_sys", "op": ">=", "value": 180}]})
        assert created.status_code == 201, created.text
    return {"patient": patient, "county": county}


def test_命中两条_按规则编号取第一条开单(client, admin, world):
    got = client.post(f"{B}/referral-rules/check", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "extra": {"bp_sys": 190},
        "auto_create": True})
    assert got.status_code == 200, got.text
    body = got.json()
    codes = [h["rule"]["code"] for h in body["hits"] if h["rule"]["code"].startswith("P2304_")]
    assert codes == ["P2304_HTN", "P2304_ANY"]   # 与规则清单同一个次序
    assert body["case"]["target_org_id"] == world["county"]


def test_试算逐条求值的规则查询带排序_静态钉():
    from app.spd.routers import referral

    tree = ast.parse(textwrap.dedent(inspect.getsource(referral.check_referral_rules)))
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]
    iterated = [ast.unparse(loop.iter) for loop in loops]
    assert any("order_by(SpdReferralRule.id)" in text for text in iterated), iterated   # 修前是 query.all()
