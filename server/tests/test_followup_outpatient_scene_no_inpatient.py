"""门诊场景的「自动匹配」不把入院登记建的住院就诊扫进来（P2-918，第二十五批「搜索与模糊匹配」扫描 J4-4，主题外顺带）。

`auto_match_plans` 的门诊 / 术后 / 体检场景都从 `Encounter` 取候选、不筛 `encounter_type`；入院登记会建一条
`encounter_type="inpatient"` 的就诊。门诊场景照扫：`scene=outpatient scanned 2 matched 2`，住院患者按入院日 +7 天被排上
门诊随访（这时多半还在院），出院后住院方案还会再派一套。门急诊文书早就不收住院就诊（P2-264）。修后门诊场景排除住院类
就诊；术后、体检场景借用门诊就诊的口径另见 P2-91。
"""
from app.database import SessionLocal
from app.spd.models import SpdFollowupRecord


def _records(patient):
    with SessionLocal() as db:
        return db.query(SpdFollowupRecord).filter(SpdFollowupRecord.patient_id == patient).count()


def test_门诊自动匹配不给住院患者排门诊随访(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2918 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2918 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2918-1"}).json()["id"]
    rule = client.post("/api/spd/followup-rules", headers=admin, json={
        "code": "P2918_OUT", "name": "P2918 心衰门诊随访", "scene": "outpatient",
        "diagnosis_keywords": ["心功能不全"], "points": [7]})
    assert rule.status_code == 201, rule.text
    inpatient = client.post("/api/patients", headers=admin, json={
        "name": "P2918 住院患者", "id_card": "330102196101012918"}).json()["id"]
    outpatient = client.post("/api/patients", headers=admin, json={
        "name": "P2918 门诊患者", "id_card": "330102196202022918"}).json()["id"]
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": inpatient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "慢性心功能不全"})
    assert admitted.status_code == 201, admitted.text
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": outpatient, "org_id": org, "diagnosis_name": "慢性心功能不全"})
    assert visit.status_code == 201, visit.text
    got = client.post("/api/spd/followup-plans/auto-match", headers=admin,
                      json={"scene": "outpatient", "org_id": org, "days": 7})
    assert got.status_code == 200, got.text
    assert (_records(inpatient), _records(outpatient)) == (0, 1)   # 修前 (1, 1)：在院患者被排上门诊随访
