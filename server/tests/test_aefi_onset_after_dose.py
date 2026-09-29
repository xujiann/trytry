"""AEFI 关联的那一剂不能晚于发病（P2-884，第二十四批「时间窗口的边界」扫描 Z1-3）。

`report_aefi` 带 `record_id` 时只查接种记录存在、属于该患者，然后从记录带出疫苗与批号；docstring 写明关联剂次是为了
「查得出是哪一针引起的」。9-20 接种、批号 HB2026A 的那一剂，挂上一例 9-10 发病的「严重反应」照收 201——这一批凭空多一
例严重反应，还牵动封存判断。修后发病早于接种日的 422；同日照收。发病距接种多久还算这一剂（监测窗口）不在本条。
"""

B = "/api/vaccine-supply"


def test_发病早于接种_422_同日照收(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2884 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2884 受种者", "id_card": "330102201801012884"}).json()["id"]
    record = client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": patient, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "dose_no": 2,
        "vaccinated_date": "2026-09-20", "org_id": org})
    assert record.status_code == 201, record.text

    def report(onset):
        return client.post(f"{B}/aefi", headers=admin, json={
            "patient_id": patient, "record_id": record.json()["id"], "reaction_type": "severe",
            "symptom": "过敏性皮疹", "onset_date": onset, "org_id": org})

    got = report("2026-09-10")
    assert got.status_code == 422, got.text   # 修前 201：9-10 发病的严重反应记到 9-20 这一剂上
    assert "早于这一剂的接种日期 2026-09-20" in got.json()["detail"]
    same_day = report("2026-09-20")
    assert same_day.status_code == 201, same_day.text   # 同日照收
    assert same_day.json()["vaccine_code"] == "HepB"
