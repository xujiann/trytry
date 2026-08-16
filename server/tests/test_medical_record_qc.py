"""块2：结构化病历与环节质控——缺陷检出、评分分级、修正后复评提分、统计口径。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

GOOD_ILLNESS = (
    "患者3天前无明显诱因出现咳嗽咳痰，痰白质黏，伴发热，最高体温38.5℃，"
    "自服退热药后可暂缓，无胸痛咯血，无夜间盗汗，饮食睡眠尚可，二便正常，体重无明显变化。"
)
GOOD_BASIS = "根据患者咳嗽发热病史、肺部湿啰音体征及胸片提示右下肺斑片影，符合社区获得性肺炎诊断标准。"
GOOD_RECORD = {
    "chief_complaint": "咳嗽发热3天",
    "present_illness": GOOD_ILLNESS,
    "past_history": "否认高血压糖尿病史，否认药物过敏史",
    "physical_exam": "体温38.2℃，血压126/78mmHg，脉搏92次/分，右下肺可闻及湿啰音",
    "diagnosis_basis": GOOD_BASIS,
    "treatment_plan": "予头孢呋辛抗感染用药，止咳化痰对症处置，3天后门诊随访复查",
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "环节质控县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={
            "username": "mr_doc",
            "password": "pass123456",
            "full_name": "陈医生",
            "role": "doctor",
            "org_id": org["id"],
        },
        headers=admin,
    )
    client.post(
        "/api/users",
        json={
            "username": "mr_doc2",
            "password": "pass123456",
            "full_name": "赵医生",
            "role": "doctor",
            "org_id": org["id"],
        },
        headers=admin,
    )
    doctor = login(client, "mr_doc", "pass123456")
    doctor2 = login(client, "mr_doc2", "pass123456")
    return {"org": org, "doctor": doctor, "doctor2": doctor2}


def new_encounter(client, headers, base, id_card, name):
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1980-01-01"},
        headers=headers,
    ).json()
    encounter = client.post(
        "/api/encounters",
        json={
            "patient_id": patient["id"],
            "org_id": base["org"]["id"],
            "doctor_name": "陈医生",
            "diagnosis_name": "社区获得性肺炎",
        },
        headers=headers,
    ).json()
    return patient, encounter


def test_rule_library_seeded(client, admin):
    rules = client.get("/api/quality/record-qc-rules", headers=admin).json()
    assert len(rules) == 12
    codes = {r["code"] for r in rules}
    assert {"MRQC01", "MRQC03", "MRQC11", "MRQC12"} <= codes
    chief = next(r for r in rules if r["code"] == "MRQC02")
    assert chief["rule"] == "max_length" and chief["config"]["max"] == 20


def test_complete_record_scores_grade_a(client, base):
    _, encounter = new_encounter(client, base["doctor"], base, "330281198001019011", "张完整")
    resp = client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], **GOOD_RECORD},
        headers=base["doctor"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["qc"]["defects"] == [] and body["qc"]["score"] == 100
    assert body["qc"]["grade"] == "甲"
    assert body["record"]["doctor_name"] == "陈医生"


def test_defects_detected_and_graded_c(client, base):
    _, encounter = new_encounter(client, base["doctor"], base, "330281198002029019", "李缺陷")
    resp = client.post(
        "/api/quality/records",
        json={
            "encounter_id": encounter["id"],
            "chief_complaint": "",  # 主诉未填
            "present_illness": "咳嗽",  # 少于50字
            "past_history": "",  # 既往史未填
            "physical_exam": "",  # 体格检查未填（含生命体征关键词缺失）
            "diagnosis_basis": "",  # 诊断依据未填（且不足30字）
            "treatment_plan": "",  # 治疗方案未填
        },
        headers=base["doctor"],
    )
    qc = resp.json()["qc"]
    codes = {d["rule_code"] for d in qc["defects"]}
    assert {"MRQC01", "MRQC03", "MRQC05", "MRQC06", "MRQC08", "MRQC09", "MRQC10"} <= codes
    # 未触发条件的规则不参与（无危急值报告/未出院）
    assert "MRQC11" not in codes and "MRQC12" not in codes
    assert qc["deducted"] == sum(d["deduct_points"] for d in qc["defects"])
    assert qc["score"] == max(0, 100 - qc["deducted"])
    assert qc["grade"] == "丙"
    defect_fields = {d["field_name"] for d in qc["defects"]}
    assert "主诉" in defect_fields and "治疗方案" in defect_fields


def test_chief_complaint_max_length_and_grade_b(client, base):
    _, encounter = new_encounter(client, base["doctor"], base, "330281198003039013", "王超长")
    body = {
        **GOOD_RECORD,
        "chief_complaint": "反复咳嗽咳痰伴发热气促胸闷乏力食欲下降体重减轻已有三天余",  # >20字
        "past_history": "",  # 既往史未填
        "diagnosis_basis": "肺炎",  # 不足30字（必填已满足）
    }
    qc = client.post(
        "/api/quality/records", json={"encounter_id": encounter["id"], **body}, headers=base["doctor"]
    ).json()["qc"]
    codes = {d["rule_code"] for d in qc["defects"]}
    assert codes == {"MRQC02", "MRQC05", "MRQC09"}
    assert qc["deducted"] == 3 + 6 + 5 and qc["score"] == 86 and qc["grade"] == "乙"


def test_update_and_rescore_raises_score(client, base):
    _, encounter = new_encounter(client, base["doctor"], base, "330281198004049016", "钱整改")
    first = client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], "chief_complaint": "发热2天"},
        headers=base["doctor"],
    ).json()
    record_id = first["record"]["id"]
    assert first["qc"]["grade"] == "丙"
    low_score = first["qc"]["score"]
    # 修正病历 → 同一就诊仍为一份（更新而非新增）
    fixed = client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], **GOOD_RECORD},
        headers=base["doctor"],
    ).json()
    assert fixed["created"] is False and fixed["record"]["id"] == record_id
    assert fixed["qc"]["score"] > low_score and fixed["qc"]["grade"] == "甲"
    # 复评接口口径与提交一致
    rescored = client.get(f"/api/quality/records/{record_id}/qc", headers=base["doctor"]).json()
    assert rescored["score"] == fixed["qc"]["score"] and rescored["defects"] == []
    detail = client.get(f"/api/quality/records/{record_id}", headers=base["doctor"]).json()
    assert detail["record"]["qc_grade"] == "甲" and detail["defects"] == []
    assert client.get("/api/quality/records/999999/qc", headers=base["doctor"]).status_code == 404


def test_critical_value_disposal_rule_conditional(client, admin, base):
    patient, encounter = new_encounter(client, base["doctor"], base, "330281198005059010", "孙危急")
    # 无危急值报告时：治疗方案不含处置关键词也不判 MRQC11
    plain = {**GOOD_RECORD}
    qc = client.post(
        "/api/quality/records", json={"encounter_id": encounter["id"], **plain}, headers=base["doctor"]
    ).json()["qc"]
    assert all(d["rule_code"] != "MRQC11" for d in qc["defects"])
    # 产生危急值报告后条件触发
    req = client.post(
        "/api/exams",
        json={
            "patient_id": patient["id"],
            "from_org_id": base["org"]["id"],
            "center_type": "lab",
            "item_code": "K",
            "item_name": "血钾",
        },
        headers=base["doctor"],
    ).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=base["doctor"])
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"finding": "血钾 6.8mmol/L", "conclusion": "高钾血症", "critical": True},
        headers=base["doctor"],
    )
    after = client.get(
        f"/api/quality/records/{client.get('/api/quality/records?encounter_id=' + str(encounter['id']), headers=base['doctor']).json()[0]['id']}/qc",
        headers=base["doctor"],
    ).json()
    assert any(d["rule_code"] == "MRQC11" for d in after["defects"])
    assert after["score"] == 90 and after["grade"] == "甲"
    # 补记危急值处置后缺陷消失
    fixed = client.post(
        "/api/quality/records",
        json={
            "encounter_id": encounter["id"],
            **{**GOOD_RECORD, "treatment_plan": GOOD_RECORD["treatment_plan"] + "；已接危急值通知并完成紧急处置"},
        },
        headers=base["doctor"],
    ).json()["qc"]
    assert fixed["defects"] == [] and fixed["score"] == 100


def test_qc_summary_by_org_and_doctor(client, admin, base):
    org2 = client.post(
        "/api/organizations",
        json={"name": "环节质控卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "周乡镇", "id_card": "330281198006069018", "gender": "女", "birth_date": "1980-06-06"},
        headers=admin,
    ).json()
    # 赵医生须归属 org2 才能在本机构写就诊记录（横向隔离：不得以他院名义建档）
    client.post(
        "/api/users",
        json={
            "username": "mr_doc2_org2",
            "password": "pass123456",
            "full_name": "赵医生",
            "role": "doctor",
            "org_id": org2["id"],
        },
        headers=admin,
    )
    doc2_org2 = login(client, "mr_doc2_org2", "pass123456")
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org2["id"], "diagnosis_name": "上呼吸道感染"},
        headers=doc2_org2,
    ).json()
    client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], "chief_complaint": "咽痛1天"},
        headers=doc2_org2,
    )
    summary = client.get("/api/quality/records/qc-summary", headers=admin).json()
    assert summary["total"] >= 6 and summary["period"] == "累计"
    assert sum(summary["grade_distribution"].values()) == summary["total"]
    total_by_org = sum(o["total"] for o in summary["by_org"])
    total_by_doctor = sum(d["total"] for d in summary["by_doctor"])
    assert total_by_org == summary["total"] == total_by_doctor
    zhao = next(d for d in summary["by_doctor"] if d["name"] == "赵医生")
    assert zhao["total"] == 1 and zhao["grade_c"] == 1 and zhao["grade_a_pct"] == 0.0
    # 机构过滤：仅统计乡镇卫生院一份
    scoped = client.get(f"/api/quality/records/qc-summary?org_id={org2['id']}", headers=admin).json()
    assert scoped["total"] == 1 and scoped["by_org"][0]["name"] == "环节质控卫生院"
    assert round(scoped["avg_score"], 1) == round(scoped["by_org"][0]["avg_score"], 1)
    # 期间过滤
    assert client.get("/api/quality/records/qc-summary?period=1999-01", headers=admin).json()["total"] == 0
    assert client.get("/api/quality/records/qc-summary?period=bad", headers=admin).status_code == 422


def test_rule_toggle_changes_score_and_permissions(client, admin, base):
    _, encounter = new_encounter(client, base["doctor"], base, "330281198007079011", "吴规则")
    before = client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], **{**GOOD_RECORD, "past_history": ""}},
        headers=base["doctor"],
    ).json()["qc"]
    assert before["score"] == 94
    rules = client.get("/api/quality/record-qc-rules", headers=admin).json()
    rule = next(r for r in rules if r["code"] == "MRQC05")
    # 停用既往史必填规则后复评不再扣分
    client.patch(f"/api/quality/record-qc-rules/{rule['id']}", json={"active": False}, headers=admin)
    record_id = client.get(
        f"/api/quality/records?encounter_id={encounter['id']}", headers=admin
    ).json()[0]["id"]
    after = client.get(f"/api/quality/records/{record_id}/qc", headers=base["doctor"]).json()
    assert after["score"] == 100 and after["rules_checked"] == 11
    client.patch(f"/api/quality/record-qc-rules/{rule['id']}", json={"active": True}, headers=admin)
    # 非管理员不可改规则；非医师不可写病历
    assert client.patch(
        f"/api/quality/record-qc-rules/{rule['id']}", json={"active": False}, headers=base["doctor"]
    ).status_code == 403
    client.post(
        "/api/users",
        json={"username": "mr_op", "password": "pass123456", "role": "operator", "org_id": base["org"]["id"]},
        headers=admin,
    )
    operator = login(client, "mr_op", "pass123456")
    assert client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], **GOOD_RECORD},
        headers=operator,
    ).status_code == 403
    # 就诊不存在
    assert client.post(
        "/api/quality/records", json={"encounter_id": 999999, **GOOD_RECORD}, headers=base["doctor"]
    ).status_code == 404


def test_case_summary_rule_triggers_after_discharge(client, admin, base):
    """出院条件触发 MRQC12：历史遗留（无病案首页）出院记录判缺陷，补首页后消除。"""
    from app.database import SessionLocal
    from app.models import Admission, CaseSummary

    patient, encounter = new_encounter(client, base["doctor"], base, "330281198008089013", "郑出院")
    record = client.post(
        "/api/quality/records",
        json={"encounter_id": encounter["id"], **GOOD_RECORD},
        headers=base["doctor"],
    ).json()
    assert record["qc"]["score"] == 100  # 未出院时该规则不参与
    # 模拟历史系统迁入的出院记录（本平台出院前置校验要求首页，故直接构造遗留数据）
    db = SessionLocal()
    try:
        admission = Admission(
            patient_id=patient["id"],
            org_id=base["org"]["id"],
            ward_id=1,
            bed_id=1,
            status="discharged",
            created_by=1,
        )
        db.add(admission)
        db.commit()
        admission_id = admission.id
    finally:
        db.close()
    flagged = client.get(f"/api/quality/records/{record['record']['id']}/qc", headers=admin).json()
    assert any(d["rule_code"] == "MRQC12" for d in flagged["defects"])
    assert flagged["score"] == 88 and flagged["grade"] == "乙"
    # 补录病案首页后复评恢复
    db = SessionLocal()
    try:
        db.add(CaseSummary(admission_id=admission_id, discharge_diagnosis="社区获得性肺炎"))
        db.commit()
    finally:
        db.close()
    healed = client.get(f"/api/quality/records/{record['record']['id']}/qc", headers=admin).json()
    assert all(d["rule_code"] != "MRQC12" for d in healed["defects"]) and healed["score"] == 100
