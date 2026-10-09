"""随访周期不只认写死的 6 个指标：尿酸、糖化、定性目标上配的周期同样生效（P2-1639，第四十八批「慢专病配置域」扫描 AL2-1）。

`_followup_interval` 的说明写「随访周期取该病种当前阶段的管理目标配置」，实现却只在 `bp_sys / glucose_fasting / spo2 /
egfr / ldl / bmi` 这 6 个指标里找本阶段 → 不分阶段的目标。管理目标的新增表单与清单对任何指标都收、都显示随访周期：
县里新配一个痛风病种，治疗期尿酸目标 ≤360、随访 30 天，清单上写着 30 天，办结随访后「下次随访」却排在 90 天后；
目标配在糖化、舒张压、肌酐、餐后血糖或定性指标上的同样被忽略。

修法：6 个指标的次序原样保留（本阶段 → 不分阶段），两级都没配这 6 个的，再按本阶段 → 不分阶段取其余启用目标里
编号最小的那条。存量（落在 6 个指标上的）病种周期不变——下面最后两条是特征化。
"""
from datetime import timedelta

import pytest

from app import clock

B = "/api/spd"


def _program(client, admin, code, targets):
    program = client.post(f"{B}/programs", headers=admin, json={"code": code, "name": f"{code} 病种", "category": "chronic"})
    assert program.status_code == 201, program.text
    ids = []
    for body in targets:
        made = client.post(f"{B}/programs/{program.json()['id']}/targets", headers=admin, json=body)
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    return ids


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21639 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    _program(client, admin, "p21639_gout", [
        {"stage": "treat", "metric": "ua", "metric_name": "血尿酸", "target_high": 360, "unit": "umol/L",
         "followup_interval_days": 30},
    ])
    _program(client, admin, "p21639_hba1c", [
        {"stage": "", "metric": "hba1c", "metric_name": "糖化血红蛋白", "target_high": 7, "unit": "%",
         "followup_interval_days": 60},
    ])
    _program(client, admin, "p21639_qual", [
        {"stage": "stable", "metric": "stable_condition", "metric_name": "病情稳定", "kind": "qualitative",
         "qualitative": "无急性发作", "followup_interval_days": 45},
    ])
    # 同一级里两条非 6 指标的目标：取编号小的那条
    _program(client, admin, "p21639_two", [
        {"stage": "treat", "metric": "creatinine", "target_high": 133, "followup_interval_days": 20},
        {"stage": "treat", "metric": "ua", "target_high": 360, "followup_interval_days": 40},
    ])
    # 停用的不算
    inactive = _program(client, admin, "p21639_off", [
        {"stage": "treat", "metric": "ua", "target_high": 360, "followup_interval_days": 30},
    ])
    off = client.patch(f"{B}/targets/{inactive[0]}", headers=admin, json={"active": False})
    assert off.status_code == 200, off.text
    # 特征化：本阶段同时配了收缩压与（编号更小的）尿酸，照旧按收缩压
    _program(client, admin, "p21639_mixed", [
        {"stage": "treat", "metric": "ua", "target_high": 360, "followup_interval_days": 15},
        {"stage": "treat", "metric": "bp_sys", "target_high": 140, "followup_interval_days": 30},
    ])
    # 特征化：不分阶段配了收缩压，本阶段只有尿酸——6 个指标两级先走完，照旧按不分阶段的收缩压
    _program(client, admin, "p21639_legacy", [
        {"stage": "treat", "metric": "ua", "target_high": 360, "followup_interval_days": 15},
        {"stage": "", "metric": "bp_sys", "target_high": 140, "followup_interval_days": 60},
    ])
    return {"org": org}


def _next_followup_after_done(client, admin, world, program, stage, n):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdTask

    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21639 患者{n}", "id_card": f"33010619720101{n:04d}", "gender": "男"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=program, org_id=world["org"], status="active",
                                   stage=stage)
        db.add(enrollment)
        db.flush()
        task = SpdTask(patient_id=patient, enrollment_id=enrollment.id, program_code=program, org_id=world["org"],
                       title="P21639 随访", task_type="followup", status="pending")
        db.add(task)
        db.commit()
        task_id, enrollment_id = task.id, enrollment.id
    done = client.post(f"{B}/tasks/{task_id}/complete", headers=admin, json={"result": {"note": "已随访"}})
    assert done.status_code == 200, done.text
    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).next_followup_at


def _in(days):
    return (clock.today() + timedelta(days=days)).isoformat()


def test_痛风治疗期按尿酸目标的30天排(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_gout", "treat", 1) == _in(30)   # 修前 90


def test_只配糖化的病种_不分阶段的周期生效(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_hba1c", "treat", 2) == _in(60)   # 修前 90


def test_定性目标上配的周期生效(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_qual", "stable", 3) == _in(45)   # 修前 90


def test_同一级几条非6指标的目标_取编号小的(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_two", "treat", 4) == _in(20)   # 修前 90


def test_停用的目标不算_本阶段没别的照旧90天(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_off", "treat", 5) == _in(90)


def test_特征化_本阶段配了收缩压的照旧按收缩压(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_mixed", "treat", 6) == _in(30)


def test_特征化_不分阶段配了收缩压的照旧按它(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "p21639_legacy", "treat", 7) == _in(60)
