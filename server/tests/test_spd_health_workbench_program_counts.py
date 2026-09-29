"""卫健工作台带病种时，core 其余计数与三级能力表的在管数也按这个病种（P2-847，第二十三批「部分之和 vs 整体」扫描 Y4-6）。

P2-648 让四个工作台都收 `program_code`，卫健端当时只补了随访、转诊、路径三组；同一个 core 里的「自我管理」（在管档案里
低危的那部分）、筛查、疑似、目标池、服务人数 / 人次，以及三级能力表的在管数仍是全病种——按甲病种看，「在管 1 人」旁边
是「自我管理 3 人」，子集比全集大，三级能力表的在管数之和也不等于在管数。页面眼下不送病种，接口直调时就是这样。
修后带了病种的都按它筛；不带病种时各数不变（建档居民、团队不按病种分）。
"""
import pytest

A, B = "P2847_A", "P2847_B"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdCandidate, SpdEnrollment, SpdProgram, SpdScreening, SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2847 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2847 患者{n}", "id_card": f"33028119780808284{n}"}).json()["id"] for n in range(4)]
    with SessionLocal() as db:
        db.add_all([SpdProgram(code=A, name="P2847 甲病种"), SpdProgram(code=B, name="P2847 乙病种")])
        # 甲病种高危 1 人；乙病种低危 3 人
        for patient, code, risk in zip(patients, (A, B, B, B), ("high", "low", "low", "low")):
            db.add(SpdEnrollment(patient_id=patient, program_code=code, org_id=org, status="active", risk_level=risk))
            db.add(SpdScreening(patient_id=patient, program_code=code, source="active", org_id=org, result="suspect"))
            db.add(SpdCandidate(patient_id=patient, program_code=code, status="target", org_id=org))
            db.add(SpdTask(patient_id=patient, program_code=code, org_id=org, title="P2847 随访", status="done"))
        db.commit()
    return {"org": org}


def _core(client, admin, **params):
    resp = client.get("/api/spd/workbench/health-commission", headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_带病种_自我管理筛查目标池服务人次都按病种(client, admin, world):
    body = _core(client, admin, program_code=A)
    core = body["core"]
    assert core["enrolled"] == 1
    assert core["self_managed"] == 0, core   # 修前是全病种的低危在管（至少乙病种那 3 人），子集比全集大
    assert (core["screened"], core["suspect"], core["candidates"]) == (1, 1, 1), core
    assert (core["service_persons"], core["service_times"]) == (1, 1), core
    assert sum(row["enrolled"] for row in body["by_level"].values()) == core["enrolled"], body["by_level"]


def test_不带病种_各数照旧是全病种(client, admin, world):
    core = _core(client, admin, org_id=world["org"])["core"]
    assert (core["enrolled"], core["self_managed"], core["screened"], core["candidates"],
            core["service_persons"], core["service_times"]) == (4, 3, 4, 4, 4, 4), core
