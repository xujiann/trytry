"""审方规则种子里的抗菌药标「抗菌药物」（P1-244，第三十七批「种子与初始化数据」扫描 AA4-1）。

修前种子 50 条里的 7 味抗菌药（J01CA04 / J01CR02 / J01DC02 / J01DD04 / J01FA10 / J01MA12 / J02AC01）都没写 antibiotic、
ddd，取模型缺省 antibiotic=False、ddd=0：规则库里 antibiotic=True 的一条都没有。实测开阿莫西林 3000 mg × 5 天，
当月抗菌药物使用强度（drug-use，写进考核）DDDs 0.0、未覆盖 0——既不算、也不报「DDD 未维护」，与模型注释
「ddd 为 0 表示未维护，统计时跳过并计入未覆盖数」、drug-use docstring「不按 0 算也不悄悄丢掉」两头对不上；
规则页「抗菌/DDD」列对阿莫西林显示「—」。

修后 J01 抗细菌、J02 抗真菌一律标 antibiotic；DDD 按给药途径核定前留 0，于是开出去的方计入未覆盖、强度页明说没维护。
"""
from app import clock
from app.data.drug_rules_seed import SEED_DRUG_RULES

#: ATC 的抗细菌（J01）、抗真菌（J02）大类：《抗菌药物临床应用管理办法》所称抗菌药物
ANTIMICROBIAL_ATC = ("J01", "J02")


def test_种子里J01与J02开头的规则都标了抗菌药物():
    """静态钉住种子本身：以后往种子里加 J01 / J02 的药，漏标同样在这里红。"""
    seeded = [r for r in SEED_DRUG_RULES if r["drug_code"].startswith(ANTIMICROBIAL_ATC)]
    assert seeded, "种子里应当有 J01 / J02 的抗菌药规则"
    unflagged = [r["drug_code"] for r in seeded if r.get("antibiotic") is not True]
    assert unflagged == [], f"这些抗菌药规则没标 antibiotic=True：{unflagged}"


def test_空库启动后J01与J02种子规则都是抗菌药物(client, admin):
    resp = client.get("/api/prescriptions/rules", headers=admin)
    assert resp.status_code == 200, resp.text
    abx = {r["drug_code"]: (r["antibiotic"], r["ddd"]) for r in resp.json()
           if r["drug_code"].startswith(ANTIMICROBIAL_ATC)}
    assert abx, "空库启动后应当种出 J01 / J02 的规则"
    # DDD 种子不代填（随给药途径不同），核定前是 0——规则页显示「抗菌药物 / DDD 未维护」而不是「—」
    assert abx == dict.fromkeys(abx, (True, 0.0)), abx


def test_开阿莫西林处方_使用强度报未维护DDD而不是悄悄丢掉(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1244 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1244 肺炎患者", "id_card": "330127196001011244"}).json()["id"]
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "社区获得性肺炎",
        "items": [{"drug_code": "J01CA04", "drug_name": "阿莫西林", "daily_dose": 3000, "days": 5}]})
    assert rx.status_code == 201 and rx.json()["status"] == "auto_passed", rx.text

    resp = client.get("/api/analytics/drug-use", headers=admin,
                      params={"period": clock.today().isoformat()[:7], "org_id": org})
    assert resp.status_code == 200, resp.text
    row = next(o for o in resp.json()["orgs"] if o["org_id"] == org)
    # 修前 (0.0, 0)：这张抗菌药处方既不进 DDDs，也不进未覆盖数
    assert (row["antibiotic_ddds"], row["ddd_uncovered_items"]) == (0.0, 1), row
