"""抗菌药物使用强度不算药师退回的处方（P2-60）。

使用强度 = Σ(日剂量×天数 ÷ DDD) × 100 ÷ 同期收治人天，写进考核。分子原先按期间内全部处方明细求和，
药师退回的也在内。2026-09-24 开发库实测（修前代码）：抗菌药（DDD=1）超日剂量开 3×5 天 → 待药师审 →
退回，这张没用上的处方照样给该院记了 15 DDDs；医生改量重开一张，同一疗程就算了两遍。

修法：分子排除已退回的处方；待审的照算（统计现算，之后被退回自然就掉出去）。
"""
from app import clock


def test_药师退回的抗菌药处方不计入DDDs(client, admin):
    org = client.post("/api/organizations", json={"name": "使用强度卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    pid = client.post("/api/patients", json={"name": "使用强度患者", "id_card": "330192198501010019",
                                             "gender": "女", "birth_date": "1985-01-01"}, headers=admin).json()["id"]
    r = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": "P260-ABX", "max_daily_dose": 2, "antibiotic": True, "ddd": 1})
    assert r.status_code == 201, r.text

    def prescribe(daily_dose):
        rx = client.post("/api/prescriptions", headers=admin, json={"patient_id": pid, "org_id": org, "items": [
            {"drug_code": "P260-ABX", "drug_name": "使用强度头孢", "daily_dose": daily_dose, "days": 5}]})
        assert rx.status_code == 201, rx.text
        return rx.json()

    over = prescribe(3)  # 超日剂量 → 待药师审
    assert over["status"] == "pending_review", over
    rej = client.post(f"/api/prescriptions/{over['id']}/review", json={"approve": False, "comment": "超量"},
                      headers=admin)
    assert rej.status_code == 200 and rej.json()["status"] == "rejected", rej.text
    prescribe(2)  # 医生改量重开，系统审通过：2×5 天 ÷ DDD 1 = 10

    use = client.get(f"/api/analytics/drug-use?period={clock.today().isoformat()[:7]}", headers=admin)
    assert use.status_code == 200, use.text
    body = use.json()
    rows = body["orgs"] if isinstance(body, dict) else body
    mine = [row for row in rows if row["org_id"] == org]
    assert mine and mine[0]["antibiotic_ddds"] == 10.0, mine  # 修前 25.0：退回的 15 也算了进去
