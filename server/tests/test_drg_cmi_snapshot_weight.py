"""DRG 统计的 MDC CMI 与机构 CMI 同用病例入组时的权重快照，调权只作用于此后入组的病例（P2-181）。

机构 CMI 按病例快照 `drg_weight` 算（病案首页打印的也是它），MDC CMI 却拿分组目录的现价乘例数——管理员在页面上
「调权」之后，同一页上两个 CMI 对不上：机构的不动、MDC 的整段跟着变，「Σ机构(CMI×入组例数) = Σ MDC(CMI×例数)」
这个恒等式就此不成立。
"""
import pytest


@pytest.fixture(scope="module")
def seeded(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2181 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2181 呼吸科"}).json()["id"]
    for i in range(3):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2181-{i}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2181 患者{i}", "id_card": f"33010619600101{2181 + i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "社区获得性肺炎"}).json()["id"]
        summary = client.post(f"/api/inpatient/admissions/{adm}/case-summary", headers=admin, json={
            "discharge_diagnosis": "社区获得性肺炎", "total_cost": 6000, "outcome": "好转"})
        assert summary.status_code == 201, summary.text
    return {"org": org, "code": summary.json()["drg_code"], "weight": summary.json()["drg_weight"]}


def _stats(client, admin, seeded):
    body = client.get("/api/drgs/stats", headers=admin).json()
    org_row = next(o for o in body["orgs"] if o["org_id"] == seeded["org"])
    mdc_code = next(g["mdc"] for g in body["groups"] if g["drg_code"] == seeded["code"])
    mdc_row = next(m for m in body["mdcs"] if m["mdc"] == mdc_code)
    return org_row["cmi"], mdc_row["cmi"]


def test_调权之后两个CMI仍按入组时的权重算_对得上(client, admin, seeded):
    assert _stats(client, admin, seeded) == (seeded["weight"], seeded["weight"])
    group = next(g for g in client.get("/api/drgs/groups", headers=admin).json() if g["code"] == seeded["code"])
    patched = client.patch(f"/api/drgs/groups/{group['id']}", headers=admin,
                           json={"base_weight": round(seeded["weight"] + 0.15, 2)})
    assert patched.status_code == 200, patched.text
    # 修前 MDC 那一项变成调过的新权重，机构的不动：同一页两个 CMI 对不上
    assert _stats(client, admin, seeded) == (seeded["weight"], seeded["weight"])
