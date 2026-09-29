"""随访质控抽查的结论只判一次：已判定的再判 409、结论与备注不动（P2-760，第二十批「状态机迁移」扫描 M4-5）。

`record_qc_result` 原先无条件覆写结论 / 方式 / 备注：质控主任判「不合格」之后，被抽查的随访医生本人（doctor 角色本就
能判）再判一次「合格」照 200、备注清空，合格率跟着变。页面对已判定的样本早就不给「判定」按钮；筛查复核（P2-736）是同
一个写法。判定人落库、挡住执行人给自己的随访判定另行登记待裁定（要加列与定规矩）。
"""
import pytest

from conftest import login

from app import models as M
from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2760 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, role in (("p2760_qc", "director"), ("p2760_doc", "doctor")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2760 患者", "id_card": "330102195001012760"}).json()["id"]
    return {"org": org, "patient": patient, "n": 0,
            "qc": login(client, "p2760_qc", "passw0rd1"), "doctor": login(client, "p2760_doc", "passw0rd1")}


def _sample(world):
    world["n"] += 1
    with SessionLocal() as db:
        record = M.SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], status="done")
        db.add(record)
        db.flush()
        sample = M.SpdQcSample(record_id=record.id, batch=f"P2760-{world['n']}")
        db.add(sample)
        db.commit()
        return sample.id


def _row(sample_id):
    with SessionLocal() as db:
        sample = db.get(M.SpdQcSample, sample_id)
        return sample.result, sample.method, sample.note


def test_判了不合格之后_被抽查的医生再判合格_409且结论不动(client, world):
    sample = _sample(world)
    first = client.post(f"{B}/qc-samples/{sample}/result", headers=world["qc"],
                        json={"result": "fail", "method": "phone", "note": "回访核实未上门"})
    assert first.status_code == 200 and first.json() == {"id": sample, "result": "fail"}, first.text
    again = client.post(f"{B}/qc-samples/{sample}/result", headers=world["doctor"],
                        json={"result": "pass", "method": "record"})
    assert again.status_code == 409, again.text   # 修前 200：不合格改成合格、备注清空
    assert again.json()["detail"] == "该样本已判定，不能重复判定"
    assert _row(sample) == ("fail", "phone", "回访核实未上门")


@pytest.mark.parametrize("result", ["pass", "warn", "fail"])
def test_未判定的照常判(client, world, result):
    sample = _sample(world)
    resp = client.post(f"{B}/qc-samples/{sample}/result", headers=world["doctor"],
                       json={"result": result, "method": "wechat", "note": "照常"})
    assert resp.status_code == 200 and resp.json() == {"id": sample, "result": result}, resp.text
    assert _row(sample) == (result, "wechat", "照常")
