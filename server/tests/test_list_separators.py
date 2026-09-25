"""「逗号分隔」的清单只认半角逗号：中文输入法填的「，」「、」整串成了一个词，永远命中不了（P1-137）。

DRG 分组的主诊断 / 主手术关键词、审方规则的相互作用药品、禁忌诊断、特殊人群都是一串「逗号分隔」的文字，后端按
`split(",")` 拆。界面（DRG 分组的增补表单）与审方规则的 JSON 导入都是人手填的——用中文输入法，逗号是全角的、列举
习惯用顿号。「鼻息肉，鼻窦炎」拆出来是**一个**关键词，病案诊断里不会整串出现，这个分组从此一个病例也入不了（全落 QY
兜底组）；禁忌诊断「妊娠，哺乳期」同样整串比对，这条禁忌从不触发——处方照样「系统审通过」，不报错也看不出来（规则表
里显示的就是填进去的原文）。修法：`texttypes.split_list` 一处认半角逗号、全角逗号、顿号，两处都改用它。
"""
import pytest

from app.texttypes import split_list


def test_三种分隔符都认_去空白去空项():
    assert split_list(" 甲，乙、丙, 丁,,") == ["甲", "乙", "丙", "丁"]
    assert split_list("") == [] and split_list(None) == []


def test_DRG关键词用全角逗号_照样入组(client, admin):
    created = client.post("/api/drgs/groups", headers=admin, json={
        "code": "P2109D", "name": "P2109 鼻部手术组", "base_weight": 0.9, "keywords": "鼻息肉，鼻窦炎、鼻中隔偏曲"})
    assert created.status_code == 201, created.text
    resp = client.post("/api/drgs/pre-check", headers=admin, json={"diagnosis": "慢性鼻窦炎", "operation": ""})
    assert resp.status_code == 200, resp.text
    assert "P2109D" in [c["code"] for c in resp.json()["candidates"]]   # 修前整串「鼻息肉，鼻窦炎、鼻中隔偏曲」比不上


@pytest.fixture(scope="module")
def rx_world(client, admin):
    from conftest import login

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2109 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p2109_doc", "password": "pass123456", "role": "doctor", "org_id": org})
    assert resp.status_code == 201, resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2109 患者", "id_card": "320981199202022109", "birth_date": "1992-02-02"}).json()["id"]
    imported = client.post("/api/prescriptions/rules/import", headers=admin, json=[{
        "drug_code": "P2109RX", "max_daily_dose": 100, "contraindicated_diagnoses": "妊娠，哺乳期"}])
    assert imported.status_code == 200, imported.text
    return {"org": org, "patient": patient, "doctor": login(client, "p2109_doc", "pass123456")}


def test_禁忌诊断用全角逗号_照样转药师审(client, rx_world):
    rx = client.post("/api/prescriptions", headers=rx_world["doctor"], json={
        "patient_id": rx_world["patient"], "org_id": rx_world["org"], "diagnosis_name": "哺乳期乳腺炎",
        "items": [{"drug_code": "P2109RX", "drug_name": "P2109 药", "daily_dose": 10}]})
    assert rx.status_code == 201, rx.text
    body = rx.json()
    assert body["status"] == "pending_review", body   # 修前 auto_passed：禁忌「妊娠，哺乳期」整串比不上
    assert "禁忌诊断" in body["review_comment"] and "哺乳期" in body["review_comment"]
