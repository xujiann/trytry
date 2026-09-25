"""设备批量上传的监测值不查病种编码：填错一个编码照样落库（P2-85，P1-120 的漏网一处）。

单条录入（`create_measurement`）与居民自报都在写库前查病种编码（P1-120）；设备批量上传（`batch_measurements`，
蓝牙 / 物联网 / HIS 一次回传多条）逐条走同一个落库帮手，却没有这一句——填错的编码原样落库，这些监测值挂到一个
不存在的病种上，按病种的监测记录、趋势与统计从此不含它们。P1-120 的闸门只看请求体模型自己的字段，批量体的编码在
`items: list[MeasurementIn]` 里一层，看不见。

修法：批量上传写库前逐个查条目里的病种编码（点名填错的，整批 404、一条不落，与生理上不可能的值整批 422 同一口径）；
闸门把请求体里一层子模型（`list[子模型]` / 子模型）的编码字段也算进来。
"""
B = "/api/spd"


def _count(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdMeasurement

    with SessionLocal() as db:
        return db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == patient_id).count()


def test_批量上传_病种编码填错_整批404一条不落(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P285 患者", "id_card": "330127197006060285"}).json()["id"]
    item = {"patient_id": patient, "metric": "bp_sys", "value": 130, "source": "device"}
    resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
        {**item, "program_code": "hypertension"}, {**item, "program_code": "hypertensoin", "metric": "bp_dia",
                                                   "value": 85}]})
    assert resp.status_code == 404 and resp.json() == {"detail": "专病档案不存在：hypertensoin"}, resp.text  # 修前 200
    assert _count(patient) == 0   # 修前两条都落了库，一条挂在不存在的病种上
    # 编码对的照收；空串是「不限病种」，照收
    resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
        {**item, "program_code": "hypertension"}, {**item, "program_code": "", "metric": "bp_dia", "value": 85}]})
    assert resp.status_code == 200 and resp.json()["created"] == 2, resp.text
