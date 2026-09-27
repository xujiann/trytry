"""慢专病工作台带病种时，同一页的数字都按这个病种（P2-648，第十四批「导出 / 打印 vs 页面」扫描 R1-10）。

卫健端、区域分析、专家端、中心端四个工作台都收 `program_code`，原先只有在管数与任务按它筛，随访、转诊、路径三组
仍是全病种（P2-552 的登记里却写着「在管数、路径、转诊都按所选病种」）；中心端的目标池、本月、生命周期、待处置上报
也是全病种——同一张工作台上「在管 1 人」、下面「转诊 37 单」。页面眼下不送这个参数，接口直调时就是这样。

修法：随访 / 转诊 / 路径三个统计收 `program_code`（路径实例经纳管档案归病种），四个工作台都传；中心端其余计数同一句筛。
"""
import pytest

A, B = "P2648_A", "P2648_B"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import (SpdCandidate, SpdCaseReport, SpdEnrollment, SpdFollowupRecord, SpdPathInstance,
                                SpdPathTemplate, SpdProgram, SpdReferralCase, SpdScreening)

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2648 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2648 患者", "id_card": "330281197808082648"}).json()["id"]
    with SessionLocal() as db:
        program = SpdProgram(code=A, name="P2648 甲病种")
        db.add(program)
        db.flush()
        template = SpdPathTemplate(program_id=program.id, code="P2648_T", name="P2648 路径")
        db.add(template)
        db.flush()
        for code in (A, B):
            enrollment = SpdEnrollment(patient_id=patient, program_code=code, org_id=org, status="active")
            db.add(enrollment)
            db.flush()
            db.add_all([
                SpdPathInstance(enrollment_id=enrollment.id, template_id=template.id, status="running"),
                SpdFollowupRecord(patient_id=patient, program_code=code, org_id=org, planned_at="2099-01-01"),
                SpdReferralCase(patient_id=patient, program_code=code, direction="up", initiator_org_id=org,
                                target_org_id=org, current_level="village", status="submitted", reason="P2648"),
                SpdCandidate(patient_id=patient, program_code=code, status="target", org_id=org),
                SpdScreening(patient_id=patient, program_code=code, source="active", org_id=org, result="suspect"),
                SpdCaseReport(patient_id=patient, program_code=code, org_id=org, content="P2648 上报"),
            ])
        db.commit()


def _get(client, admin, path):
    resp = client.get(f"/api/spd/{path}?program_code={A}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_卫健端与区域分析的随访_转诊_路径按病种(client, admin, world):
    for path in ("workbench/health-commission", "stats/region"):
        body = _get(client, admin, path)
        got = (body["followups"]["total"], body["referrals"]["total"], body["paths"]["total"])
        assert got == (1, 1, 1), (path, got)   # 修前三组都是全病种（至少 2）


def test_专家端的转诊_路径按病种(client, admin, world):
    body = _get(client, admin, "workbench/expert")
    assert (body["referrals"]["total"], body["paths"]["total"]) == (1, 1)


def test_中心端的目标池_本月_转诊_生命周期_上报按病种(client, admin, world):
    body = _get(client, admin, "workbench/center")
    assert body["referrals"]["total"] == 1
    assert (body["pool"]["target"], body["pool"]["unassigned"], body["pool"]["pending_review"]) == (1, 1, 1)
    assert body["monthly"]["new_enrollments"] == 1
    assert body["case_reports"]["pending"] == 1
