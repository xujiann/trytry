"""关键词子串比对认得出大小写、全角与空白不同的写法：DRG 的主手术 / 主诊断关键词、审方规则的禁忌诊断（P2-792，第二十一批
「同一个值的不同写法」扫描 N3-5）。

两处原先按原样找子串：手术写成 `pci术`、`ＰＣＩ术`、`急诊Pci` 都命中不了「PCI」，经皮冠脉介入落进内科组（FM29 权重 3.6 →
FM19 1.42）；诊断写成 `qt间期延长`、`ＱＴ间期延长` 命中不了阿奇霉素的禁忌「QT间期延长」，系统审通过。修法：两侧都过
`texttypes.text_key`（全角转半角、不分大小写、去空白）再比；只用于比对，不改落库的值。
"""
import pytest

from conftest import login

from app.texttypes import text_key

AZI = "P2792AZI"


def test_比对键_全角大小写空白归一():
    assert text_key(" ＰＣＩ 术") == text_key("pci术") == "pci术"
    assert text_key("ＱＴ间期　延长") == text_key("QT间期延长")


@pytest.mark.parametrize("operation", ["pci术", "ＰＣＩ术", "急诊Pci", "P CI术"], ids=["小写", "全角", "混写", "夹空格"])
def test_主手术写法不同_照样入外科组(client, admin, operation):
    baseline = client.post("/api/drgs/pre-check", headers=admin, json={
        "diagnosis": "急性心肌梗死", "operation": "PCI术"}).json()["candidates"][0]["code"]
    top = client.post("/api/drgs/pre-check", headers=admin, json={
        "diagnosis": "急性心肌梗死", "operation": operation}).json()["candidates"][0]["code"]
    assert top == baseline == "FM29", (operation, top)   # 修前落进内科组 FM19


@pytest.fixture(scope="module")
def rx_world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2792 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p2792_doc", "password": "passw0rd1", "full_name": "P2792 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2792 患者", "id_card": "330102197001012792"}).json()["id"]
    rule = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": AZI, "max_daily_dose": 500, "dose_unit": "mg", "contraindicated_diagnoses": "QT间期延长"})
    assert rule.status_code == 201, rule.text
    return {"org": org, "patient": patient, "doctor": login(client, "p2792_doc", "passw0rd1")}


def _rx(client, world, diagnosis):
    resp = client.post("/api/prescriptions", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": diagnosis,
        "items": [{"drug_code": AZI, "drug_name": "阿奇霉素", "daily_dose": 500}]})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("diagnosis", ["社区获得性肺炎；qt间期延长", "社区获得性肺炎；ＱＴ间期延长", "社区获得性肺炎；QT 间期延长"],
                         ids=["小写", "全角", "夹空格"])
def test_诊断写法不同_禁忌诊断照样判(client, rx_world, diagnosis):
    rx = _rx(client, rx_world, diagnosis)
    assert rx["status"] == "pending_review" and "禁忌诊断" in rx["review_comment"], rx   # 修前 auto_passed


def test_原样照旧_不相干的诊断照旧系统审通过(client, rx_world):
    assert _rx(client, rx_world, "社区获得性肺炎；QT间期延长")["status"] == "pending_review"
    assert _rx(client, rx_world, "社区获得性肺炎")["status"] == "auto_passed"
