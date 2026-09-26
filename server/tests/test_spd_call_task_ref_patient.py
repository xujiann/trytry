"""呼叫任务挂的随访记录不查是不是这位患者的：甲的通话结果与录音地址被回写进乙的随访记录（P2-296）。

`create_call_task` 只查患者可见，`ref_id` 照收；接通的回写（`record_call_result`）按 `ref_id` 把沟通结果与录音地址
追加进那条随访记录。界面上「转呼叫」送的是这条随访的患者与编号，对得上；接口直调时对不上也 201——甲的呼叫任务
挂上乙的随访，回写之后甲的通话内容就进了乙的档案（录音地址属于个人信息）。挂一个不存在的编号也照收，回写时静默跳过。

修法：挂随访记录的，不存在 404、不是这位患者的 422（与关联接种记录同一口径）；复诊 / 宣教等只作引用、不回写，不在此列。
与 P1-130（回写由谁来写，待裁定）是两件事：这里只管「挂得对不对」。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdFollowupRecord

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2296 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    jia, yi = (client.post("/api/patients", headers=admin, json={"name": name, "id_card": card}).json()["id"]
               for name, card in (("P2296 甲", "330127197309092296"), ("P2296 乙", "330127197309102296")))
    with SessionLocal() as db:
        records = {}
        for key, pid in (("jia", jia), ("yi", yi)):
            record = SpdFollowupRecord(patient_id=pid, org_id=org, planned_at="2026-09-26")
            db.add(record)
            db.flush()
            records[key] = record.id
        db.commit()
    return {"jia": jia, "yi": yi, **{f"rec_{k}": v for k, v in records.items()}}


def _call(client, admin, patient_id, ref_id, ref_type="followup"):
    return client.post(f"{B}/call-tasks", headers=admin, json={
        "patient_id": patient_id, "phone": "13800002296", "ref_type": ref_type, "ref_id": ref_id})


def test_甲的呼叫任务挂乙的随访_422_乙的记录不被写进甲的通话(client, admin, world):
    got = _call(client, admin, world["jia"], world["rec_yi"])
    assert got.status_code == 422, got.text   # 修前 201
    assert got.json()["detail"] == "随访记录不属于该患者"


def test_挂不存在的随访记录_404(client, admin, world):
    got = _call(client, admin, world["jia"], 99999999)
    assert got.status_code == 404 and got.json()["detail"] == "随访记录不存在", got.text   # 修前 201


def test_挂自己的随访照常_不挂随访的引用照旧不查(client, admin, world):
    ok = _call(client, admin, world["jia"], world["rec_jia"])
    assert ok.status_code == 201, ok.text
    revisit = _call(client, admin, world["yi"], 99999999, ref_type="revisit")
    assert revisit.status_code == 201, revisit.text
