"""「全部病种」的自动干预模板也会触发，病种专用的优先（P2-1601，第四十七批扫描 AK2-2 通用模板那一半）。

建干预模板的表单病种缺省「全部病种」（空串），手工下发也把空病种的模板当通用（`create_interventions` 只拦「两边都写了
病种且不一致」，页面按 `!t.program_code` 列出）；高危自动干预 `_auto_intervene` 却按 `program_code == 档案病种` 精确取
模板。2026-10-09 实测（修前代码）：只配一套「全部病种 · 极高危自动触发」的模板，高血压患者评出极高危，干预 `[]`——
建模板时 201、没有任何提示，这套模板永不触发。

修法：取模板时病种为档案病种或空串都算，病种专用的排在前面，同类里照旧取编号最小的（P2-693 的确定性不变）；在途
同模板不重复开的判据不动。低 / 中危触不触发、「高危」模板覆不覆盖极高危是另一半，待裁定，这里不碰。
"""
import pytest

B = "/api/spd"

#: 综合风险量表（种子 `assess_risk_common`，通用量表）：18 分 very_high / 11 分 high
VERY_HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "2项及以上", "selfcare": "完全依赖"}
HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "1项", "selfcare": "能自理"}


def _template(client, admin, code, program, level):
    made = client.post(f"{B}/intervention-templates", headers=admin, json={
        "code": code, "name": f"{code} 干预", "program_code": program, "category": "drug",
        "content": f"{code} 内容", "auto_risk_level": level})
    assert made.status_code == 201, made.text
    return made.json()["id"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21601 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for code in ("p21601_a", "p21601_b", "p21601_c"):
        made = client.post(f"{B}/programs", headers=admin, json={"code": code, "name": code, "category": "chronic"})
        assert made.status_code == 201, made.text
    # 通用的先建（编号更小）：病种专用的仍要排在它前面
    generic = _template(client, admin, "p21601_all_vh", "", "very_high")
    special = _template(client, admin, "p21601_a_vh", "p21601_a", "very_high")
    other = _template(client, admin, "p21601_b_high", "p21601_b", "high")
    return {"org": org, "generic": generic, "special": special, "other": other, "seq": iter(range(1, 100))}


def _assess(client, admin, world, program, answers):
    n = next(world["seq"])
    pid = client.post("/api/patients", headers=admin, json={
        "name": f"P21601 患者{n}", "id_card": f"33012719700301{n:02d}61"}).json()["id"]
    made = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": pid, "program_code": program, "org_id": world["org"], "risk_level": "low"})
    assert made.status_code == 201, made.text
    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": pid, "scale_code": "assess_risk_common", "program_code": program, "answers": answers})
    assert resp.status_code == 201, resp.text
    return [(i["template_id"], i["program_code"], i["status"]) for i in client.get(
        f"{B}/interventions", headers=admin, params={"patient_id": pid}).json()]


def test_只配了全部病种的极高危模板_极高危评估开出一条(client, admin, world):
    assert _assess(client, admin, world, "p21601_c", VERY_HIGH_ANSWERS) == [
        (world["generic"], "p21601_c", "planned")]   # 修前 []：通用的自动模板永不触发


def test_病种专用与通用同等级都有_开病种专用的(client, admin, world):
    assert _assess(client, admin, world, "p21601_a", VERY_HIGH_ANSWERS) == [
        (world["special"], "p21601_a", "planned")]   # 通用的编号更小，也不抢病种专用的


def test_只配了别的病种的模板_不开(client, admin, world):
    assert _assess(client, admin, world, "p21601_c", HIGH_ANSWERS) == []
