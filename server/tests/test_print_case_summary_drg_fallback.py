"""病案首页打印件不给 QY 兜底组印权重（P2-1279，第三十七批「种子与初始化数据」扫描 AA4-7）。

QY 兜底组收的是哪组都没入上的病例，种子头注写「CMI 统计中剔除并单列」，机构 CMI 也确实剔除了；可它的种子权重 0.50 照样
写到每例未入组病例的 drg_weight 上。修前实测：「高钾血症」落 QY，病案首页打印件「DRG 分组」一格印「QY（权重 0.5）」——
拿首页去对医保分组结算的人会当成一个权重 0.5 的组。

修后兜底病例印「未入组（QY，需病案首页复核）」，正式入组的照旧「编码（权重 x）」、字节不变。MDC 汇总的 QY 行：响应模型
`DrgMdcStatOut.cmi` 是不可空的 float，改成可空属破坏性变更，不改——保持原值，靠同一行现成的 `fallback: true` 标出它是兜底
组（页面上已打「兜底」标签）；机构 CMI 照旧剔除兜底病例。种子权重与入组写入不动（只增不改）。
"""
import re

import pytest

from app.data.drg_groups_seed import FALLBACK_DRG_GROUP


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21279 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org["id"], "name": "内一"}).json()
    cases = {}
    for i, (key, diagnosis) in enumerate((("fallback", "高钾血症"), ("grouped", "脑梗死"))):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": f"0{i}"}).json()
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21279 患者{i}", "id_card": f"33012719550101127{i}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": diagnosis}).json()
        summary = client.post(f"/api/inpatient/admissions/{adm['id']}/case-summary", headers=admin, json={
            "discharge_diagnosis": diagnosis, "total_cost": 3000, "drug_cost": 800})
        assert summary.status_code == 201, summary.text
        assert client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin).status_code == 200
        cases[key] = {"admission": adm["id"], "receipt": summary.json(), "org": org["id"]}
    return cases


def _drg_cell(client, admin, admission_id: int) -> str:
    resp = client.get(f"/api/print/case-summaries/{admission_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    cells = re.findall(r'<td class="k">DRG 分组</td><td>([^<]*)</td>', resp.text)
    assert len(cells) == 1, resp.text   # 防空转：确实取到了「DRG 分组」那一格
    return cells[0]


def test_前提_未入任何组的病例落QY_带着种子权重(world):
    receipt = world["fallback"]["receipt"]
    assert (receipt["drg"]["drg_code"], receipt["drg"]["fallback"]) == (FALLBACK_DRG_GROUP["code"], True)
    assert receipt["drg_weight"] == FALLBACK_DRG_GROUP["base_weight"]   # 入组写入不动：存量与种子只增不改


def test_兜底病例的病案首页不印权重_印未入组与复核提示(client, admin, world):
    cell = _drg_cell(client, admin, world["fallback"]["admission"])
    assert cell == "未入组（QY，需病案首页复核）", cell   # 修前「QY（权重 0.5）」
    assert "权重" not in cell


def test_正式入组的病案首页照旧印编码与权重(client, admin, world):
    receipt = world["grouped"]["receipt"]
    assert receipt["drg"]["fallback"] is False
    assert _drg_cell(client, admin, world["grouped"]["admission"]) == (
        f"{receipt['drg_code']}（权重 {receipt['drg_weight']}）")


def test_MDC汇总的QY行_类型不可空保持原值_靠fallback标出(client, admin, world):
    stats = client.get("/api/drgs/stats", headers=admin).json()
    qy = [m for m in stats["mdcs"] if m["mdc"] == FALLBACK_DRG_GROUP["mdc"]]
    assert len(qy) == 1, stats["mdcs"]
    assert qy[0]["fallback"] is True and qy[0]["cases"] == 1
    assert qy[0]["cmi"] == FALLBACK_DRG_GROUP["base_weight"]   # cmi: float 不可空，不改类型、不改值
    org = next(o for o in stats["orgs"] if o["org_id"] == world["fallback"]["org"])
    # 机构 CMI 只算正式入组（脑梗死那一例），兜底病例不进分母也不进分子
    assert (org["cases"], org["grouped"], org["fallback"]) == (2, 1, 1)
    assert org["cmi"] == world["grouped"]["receipt"]["drg_weight"]
