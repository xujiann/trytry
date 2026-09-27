"""工作台分项表不漏类：各行加起来对得上总数（P2-603，第十二批「计数 vs 清单」扫描 Z1-8）。

- 卫健端「县乡村三级服务能力」手抄县 / 乡 / 村三级，市级协作医院（机构层级 `city`）的机构、在管患者、团队哪一行都不进；
- 专家端「病种标准落地情况」只列启用的病种，停用病种的在管患者算进了「在管患者」、表里哪一行都没有；
- 专家端「覆盖机构」按全部档案数机构，已结案 / 迁出 / 死亡的档案所在机构照数，与同一排的「在管患者」不是一个范围。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdProgram

    city = client.post("/api/organizations", headers=admin, json={
        "name": "P2603 市医院", "org_type": "lead_hospital", "level": "city"}).json()["id"]
    gone = client.post("/api/organizations", headers=admin, json={
        "name": "P2603 结案院", "org_type": "township", "level": "township"}).json()["id"]
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": "p2603_stop", "name": "P2603 待停病种", "category": "specialty"})
    assert program.status_code == 201, program.text
    for n, org in enumerate((city, gone)):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2603 患者{n}", "id_card": f"33010619730303260{n}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "p2603_stop", "org_id": org})
        assert enrolled.status_code == 201, enrolled.text
        if org == gone:   # 这家的档案结了案：它不再「覆盖」
            assert client.post(f"{B}/enrollments/{enrolled.json()['id']}/lifecycle", headers=admin,
                               json={"event": "exclude", "reason": "误纳"}).status_code == 200
    with SessionLocal() as db:   # 停用病种：在管的患者照旧在管
        db.query(SpdProgram).filter(SpdProgram.code == "p2603_stop").update({"active": False})
        db.commit()
    return {"city": city, "gone": gone}


def test_卫健端三级能力表有市级一行_各行在管加起来等于在管总数(client, admin, world):
    body = client.get(f"{B}/workbench/health-commission", headers=admin).json()
    assert body["by_level"]["市级"]["orgs"] >= 1 and body["by_level"]["市级"]["enrolled"] >= 1   # 修前没有这一行
    assert sum(v["enrolled"] for v in body["by_level"].values()) == body["enrollment"]["enrolled"]


def test_专家端停用病种还有在管患者的也列出_覆盖机构只数有在管患者的(client, admin, world):
    body = client.get(f"{B}/workbench/expert", headers=admin).json()
    rows = {p["program_code"]: p for p in body["programs"]}
    assert rows["p2603_stop"]["active"] is False and rows["p2603_stop"]["enrolled"] == 1   # 修前这一行没有
    assert sum(p["enrolled"] for p in body["programs"]) == body["enrollment"]["enrolled"]
    only = client.get(f"{B}/workbench/expert", params={"program_code": "p2603_stop"}, headers=admin).json()
    assert only["org_coverage"] == 1   # 修前 2：结了案的那家照数
