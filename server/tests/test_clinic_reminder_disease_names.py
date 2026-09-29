"""诊间医防协同提醒的整句写病种中文名，不拼英文编码（P2-880，第二十四批「通知、提醒与待办」扫描 Z2-11）。

`clinic_reminders` 把病种编码原样拼进给医生看的整句：「hypertension 分级3级，建议上转评估」「diabetes 随访已超期（应访
日期 2026-06-01）」，页面原样印 `detail`。P2-767 / P2-73 已定的规矩是不把英文码拼进给人看的文字；病种目录里有中文名。
修后按病种目录取名称，目录里没有的照旧印编码。
"""
from app.database import SessionLocal
from app.models import ChronicDiseaseType, ChronicPatient


def test_超期与高危提醒写病种中文名(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2880 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2880 患者", "id_card": "330102195501012880"}).json()["id"]
    made = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert made.status_code in (200, 201), made.text
    with SessionLocal() as db:
        row = db.get(ChronicPatient, made.json()["id"])
        row.level, row.next_due = 3, "2026-06-01"
        db.commit()
        name = db.query(ChronicDiseaseType.name).filter(ChronicDiseaseType.code == "hypertension").scalar()
    got = client.get(f"/api/publichealth/reminders/{patient}", headers=admin, params={"today": "2026-08-31"})
    assert got.status_code == 200, got.text
    details = {r["type"]: r["detail"] for r in got.json()["reminders"]}
    assert details["chronic_followup_overdue"] == f"{name} 随访已超期（应访日期 2026-06-01）"   # 修前「hypertension 随访…」
    assert details["chronic_high_risk"] == f"{name} 分级3级，建议上转评估"
    assert "hypertension" not in " ".join(details.values())
