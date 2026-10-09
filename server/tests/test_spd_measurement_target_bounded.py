"""判监测值只认有界的管理目标：本阶段一条定性目标不再遮住不分阶段的量化目标（P2-1640，第四十八批「慢专病配置域」扫描
AL2-2）。

`service.target_for` 三级回落（本阶段 → 不分阶段 → 该病种任一阶段），第一级按阶段取、不看目标有没有界。建目标时只有
量化目标要求有界，定性目标（「尿酸持续达标」）上下限全空。痛风病种不分阶段配「尿酸 ≤420」、稳定期配一条尿酸的定性
目标：稳定期患者录尿酸 600，判级拿到的是那条定性目标，`judge_level` 上下限全空一律 normal——判「正常」、不派处置任务；
治疗期患者录同样的 600 照判 high、派任务。没写病种时按在管档案推断病种（`measure_program_for`）用的是同一句：先建档的
病种这个指标只配了定性目标，就挂到它下面判「正常」，后建档病种的量化目标用不上。

修法：判数值取目标只认至少有一个界的，三级回落照走；推断病种先取配了有界目标的，几个病种都只配了定性目标的照旧挂
第一个（判不了界的维持现状，判「正常」）。
"""
import pytest

B = "/api/spd"


def _idcard(body17):
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return body17 + "10X98765432"[sum(int(a) * b for a, b in zip(body17, weights)) % 11]


def _program(client, admin, code, targets):
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": code, "name": f"{code} 病种", "category": "chronic",
        "stages": [{"key": "treat", "name": "治疗期"}, {"key": "stable", "name": "稳定期"}]})
    assert program.status_code == 201, program.text
    for body in targets:
        made = client.post(f"{B}/programs/{program.json()['id']}/targets", headers=admin, json=body)
        assert made.status_code == 201, made.text


_QUAL = {"metric": "ua", "metric_name": "血尿酸", "kind": "qualitative", "qualitative": "尿酸持续达标、无痛风发作"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21640 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    _program(client, admin, "p21640_gout", [
        {"stage": "", "metric": "ua", "metric_name": "血尿酸", "target_high": 420, "unit": "umol/L"},
        {"stage": "stable", **_QUAL},
    ])
    _program(client, admin, "p21640_qual", [{"stage": "stable", **_QUAL}])   # 只有定性目标
    patients = []
    for n in range(1, 6):
        made = client.post("/api/patients", headers=admin, json={
            "name": f"P21640 患者{n}", "id_card": _idcard(f"3301061975010{n:04d}"), "gender": "男"})
        assert made.status_code in (200, 201), made.text
        patients.append(made.json()["id"])

    def enroll(patient, program, stage):
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org, "stage": stage})
        assert resp.status_code == 201, resp.text

    enroll(patients[0], "p21640_gout", "stable")
    enroll(patients[1], "p21640_gout", "treat")
    enroll(patients[2], "p21640_qual", "stable")
    enroll(patients[3], "p21640_qual", "stable")   # 先建档：这个病种的尿酸只有定性目标
    enroll(patients[3], "p21640_gout", "treat")    # 后建档：配了 ≤420
    return {"stable": patients[0], "treat": patients[1], "qual_only": patients[2], "both": patients[3]}


def _disposal_tasks(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter(SpdTask.patient_id == patient_id,
                                        SpdTask.title.like("指标异常处置%")).count()


def _record(client, admin, patient, program_code=""):
    resp = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": patient, "metric": "ua", "value": 600, "unit": "umol/L", "program_code": program_code})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_稳定期配了定性目标_照回落到不分阶段的量化目标判偏高并派任务(client, admin, world):
    out = _record(client, admin, world["stable"], "p21640_gout")
    assert out["level"] == "high"   # 修前 normal：拿稳定期的定性目标判
    assert _disposal_tasks(world["stable"]) == 1   # 修前 0


def test_治疗期照旧按不分阶段的量化目标判(client, admin, world):
    assert _record(client, admin, world["treat"], "p21640_gout")["level"] == "high"
    assert _disposal_tasks(world["treat"]) == 1


def test_只有定性目标的维持现状_判正常不派任务_没写病种也照旧挂这个病种(client, admin, world):
    assert _record(client, admin, world["qual_only"], "p21640_qual")["level"] == "normal"
    inferred = _record(client, admin, world["qual_only"])
    assert (inferred["program_code"], inferred["level"]) == ("p21640_qual", "normal")
    assert _disposal_tasks(world["qual_only"]) == 0


def test_没写病种_推断取配了有界目标的病种(client, admin, world):
    out = _record(client, admin, world["both"])
    assert (out["program_code"], out["level"]) == ("p21640_gout", "high")   # 修前 ("p21640_qual", "normal")
    assert _disposal_tasks(world["both"]) == 1   # 修前 0


def test_判级帮手直接取_定性目标不当判级目标(world):
    from app.database import SessionLocal
    from app.spd.service import judge_measurement, target_for

    with SessionLocal() as db:
        assert judge_measurement(db, "p21640_gout", "stable", "ua", 600) == "high"   # 修前 normal
        assert target_for(db, "p21640_gout", "stable", "ua").target_high == 420
        assert target_for(db, "p21640_qual", "stable", "ua") is None
        assert judge_measurement(db, "p21640_qual", "stable", "ua", 600) == "normal"
