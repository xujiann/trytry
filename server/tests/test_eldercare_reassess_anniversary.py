"""老年人年度复评提醒按周年日判「已超一年」（P2-548，第十批「日期与期间边界」扫描 X3-11）。

提醒写的是「距上次健康评估已超一年」，判据却是 `评估日 <= 今天 − 365 天`：跨过 2 月 29 日的那一年，
2023-03-01 评估的老人在 2024-02-29 就被提醒复评，比周年日早一天。修后按周年日比（2 月 29 日那天与去年 2 月 28 日比）。
"""


def _alerts(client, admin, today, patient_id):
    body = client.get(f"/api/eldercare/alerts?today={today}", headers=admin).json()
    return [a["alert_type"] for a in body["alerts"] if a["patient_id"] == patient_id]


def test_闰年里满一年按周年日算(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2548 老人", "id_card": "330106194505052548", "gender": "女", "birth_date": "1945-05-05"})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]
    resp = client.post("/api/eldercare/assessments", headers=admin, json={
        "patient_id": pid, "adl_score": 96, "assessed_date": "2023-03-01"})
    assert resp.status_code == 201, resp.text
    assert _alerts(client, admin, "2024-02-29", pid) == []   # 修前：2024-02-29 − 365 天 = 2023-03-01，提前一天报
    assert _alerts(client, admin, "2024-03-01", pid) == ["reassess_due"]
    assert _alerts(client, admin, "2024-02-28", pid) == []
