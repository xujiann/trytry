"""同一个「检查检验结果互认率」：共享诊断页保留一位，监测指标与驾驶舱保留两位（33.3% 对 33.33%）（P2-996，第二十八批
「取整与精度发生在哪一步」扫描 F2-5）。

`exams.recognition_stats` 原先 `round(recognized / total * 100, 1)`；监测指标 #5（JSON 与 CSV 同源，上报口径）与驾驶舱都按
`reports._pct` 的 `round(part * 100 / total, 2)`。三处分子分母口径相同，同一个率在两个页面差 0.03；先除后乘与先乘后除在 .x5
处结果还会不同。

修法：共享诊断页与监测指标同一个算式、同一个位数。
"""
import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def stats(client, admin):
    from app.models import ExamRequest

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2996 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2996 患者", "id_card": "330106197303030093"}).json()["id"]
    ids = []
    for i in range(3):
        made = client.post("/api/exams", headers=admin, json={
            "patient_id": patient, "from_org_id": org, "center_type": "lab", "item_code": f"P2996-{i}",
            "item_name": f"P2996 检验{i}"})
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    with SessionLocal() as db:   # 两张已报告、一张互认：互认率 1/3
        for request_id, status in zip(ids, ("reported", "reported", "recognized")):
            db.get(ExamRequest, request_id).status = status
        db.commit()
    page = client.get("/api/exams/recognition-stats", headers=admin).json()
    monitoring = client.get("/api/reports/monitoring", headers=admin).json()
    return page, monitoring


def test_共享诊断页与监测指标同一个数(stats):
    page, monitoring = stats
    assert (page["recognized_total"], page["reported_total"]) == (1, 2)
    indicator = next(i for i in monitoring["indicators"] if i["no"] == 5)
    assert page["recognition_ratio_pct"] == indicator["value"] == 33.33   # 修前共享诊断页 33.3
