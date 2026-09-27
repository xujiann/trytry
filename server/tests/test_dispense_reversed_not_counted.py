"""退药冲销的处方不算「用上了」（P2-624，第十三批「正向 vs 逆向」扫描 Q1-2）。

退药冲销把发药记录置 reversed、药回库房，处方表不动（它的状态是审方结论）；冲销后的处方不能再发，确需再发的开新处方
（`reverse_dispense` 的 docstring）。统计「用了多少药」的四处却只按处方状态筛：抗菌药物使用强度、采购建议的近 30 天
用量、居民用药画像、全县用药地图——退掉的那张照算，重开的那张再算一遍，同一疗程记两遍；只退不重开的，画像里这味药
照样「在用」、计进多重用药预警。批号追溯早就按发药记录的状态排除冲销（「冲销的不计入仍在外面的量」），这四处没跟上。
"""
import pytest

from app import clock

CODE = "P2624-ABX"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2624 退药卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2624 退药患者", "id_card": "330102198801012624", "gender": "男"}).json()["id"]
    rule = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": CODE, "max_daily_dose": 10, "antibiotic": True, "ddd": 1})
    assert rule.status_code == 201, rule.text
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "P2624 头孢", "batch_no": "P2624-B1",
        "expire_date": "2099-12-31", "quantity": 10})   # 恰好一个疗程：两张都发完库存归零，采购建议才看得见用量
    assert batch.status_code == 201, batch.text

    def prescribe_and_dispense():
        rx = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": patient, "org_id": org, "diagnosis_name": "肺炎",
            "items": [{"drug_code": CODE, "drug_name": "P2624 头孢", "daily_dose": 2, "days": 5}]})
        assert rx.status_code == 201 and rx.json()["status"] == "auto_passed", rx.text
        dispensed = client.post("/api/dispense", headers=admin, json={"prescription_id": rx.json()["id"]})
        assert dispensed.status_code == 201, dispensed.text
        return dispensed.json()["id"]

    first = prescribe_and_dispense()
    reversed_ = client.post(f"/api/dispense/{first}/reverse", headers=admin, json={"reason": "皮疹退药"})
    assert reversed_.status_code == 200 and reversed_.json()["status"] == "reversed", reversed_.text
    prescribe_and_dispense()   # 按 docstring 重开一张再发：同一疗程只该算一次
    return {"org": org, "patient": patient}


def test_抗菌药物使用强度不算退药的那张(client, admin, world):
    body = client.get(f"/api/analytics/drug-use?period={clock.today().isoformat()[:7]}", headers=admin).json()
    rows = body["orgs"] if isinstance(body, dict) else body
    (mine,) = [row for row in rows if row["org_id"] == world["org"]]
    assert mine["antibiotic_ddds"] == 10.0, mine   # 修前 20.0：退回库房的 2×5 也算进 DDDs


def test_采购建议的用量不算退药的那张(client, admin, world):
    rows = client.get("/api/pharmacy/purchase-suggestions", headers=admin).json()
    (mine,) = [r for r in rows if r["drug_code"] == CODE]
    assert (mine["usage_30d"], mine["current_stock"], mine["suggested_quantity"]) == (10.0, 0, 10), mine   # 修前 20 / 0 / 20


def test_用药画像只数用上了的处方(client, admin, world):
    profile = client.get(f"/api/medication/profile/{world['patient']}", headers=admin).json()
    (drug,) = [d for d in profile["drugs"] if d["drug_code"] == CODE]
    assert drug["times"] == 1, profile   # 修前 2：退回的那张也记一次


def test_用药地图只数用上了的处方(client, admin, world):
    rows = client.get("/api/medication/usage-stats", headers=admin).json()
    (mine,) = [r for r in rows if r["drug_code"] == CODE]
    assert (mine["rx_count"], mine["patient_count"]) == (1, 1), mine   # 修前 rx_count 2


def test_只退不重开的_画像里这味药不算在用(client, admin):
    code = "P2624-ONLY"
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2624 只退卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2624 只退患者", "id_card": "330102198801022624", "gender": "女"}).json()["id"]
    assert client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": "P2624 阿莫西林", "batch_no": "P2624-B2",
        "expire_date": "2099-12-31", "quantity": 20}).status_code == 201
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "肺炎",
        "items": [{"drug_code": code, "drug_name": "P2624 阿莫西林", "daily_dose": 2, "days": 5}]}).json()
    dispensed = client.post("/api/dispense", headers=admin, json={"prescription_id": rx["id"]}).json()
    assert client.post(f"/api/dispense/{dispensed['id']}/reverse", headers=admin,
                       json={"reason": "未服用全部退回"}).status_code == 200
    profile = client.get(f"/api/medication/profile/{patient}", headers=admin).json()
    assert [d["drug_code"] for d in profile["drugs"]] == [] and profile["in_use_drugs"] == 0, profile   # 修前在用 1
