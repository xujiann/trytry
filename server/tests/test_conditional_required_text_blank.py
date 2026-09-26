"""「这种情况下须填写」的文字只判 `not x`：一串空格照收（P2-309）。

四处条件必填的文字（字段本身带默认空串，只在某种情况下必填）判的是 `if not body.x`——空串挡住了，一串空格却当成填了：
- AEFI 上报未关联接种记录时「须填写疫苗编码」：存进一串空格，按疫苗统计归不了类；
- 死亡医学证明「须填写死因诊断」、出生缺陷登记「须填写缺陷诊断」：证明上的诊断是空白；
- 就诊凭据作废「须填写原因」：作废留痕的原因是空白；
- 绩效整改提交完成「须填写整改结果说明」：待确认队列里的说明是空白。
与 `texttypes.NON_BLANK`（必填文本不收纯空白，P1-109）同一个口径，只是这几处按情况必填、没法写在字段声明上。

修法：判 `strip()` 之后的。
"""
import pytest

PATIENT_CARD = "330127197309092309"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2309 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={"name": "P2309 患者", "id_card": PATIENT_CARD}).json()["id"]
    return {"org": org, "patient": patient}


def test_AEFI未关联接种_疫苗编码全是空格422(client, admin, world):
    got = client.post("/api/vaccine-supply/aefi", headers=admin, json={
        "patient_id": world["patient"], "vaccine_code": "   ", "symptom": "注射部位红肿", "onset_date": "2026-09-25",
        "org_id": world["org"]})
    assert got.status_code == 422 and got.json()["detail"] == "未关联接种记录时须填写疫苗编码", got.text   # 修前 201


@pytest.mark.parametrize(("cert_type", "detail"), [
    ("death", "死亡医学证明须填写死因诊断"), ("defect", "出生缺陷儿登记须填写缺陷诊断")])
def test_证明的诊断全是空格422(client, admin, world, cert_type, detail):
    got = client.post("/api/certs", headers=admin, json={
        "cert_type": cert_type, "name": "P2309 证明", "event_date": "2026-09-25", "detail": "   ",
        "org_id": world["org"], "patient_id": world["patient"]})
    assert got.status_code == 422 and got.json()["detail"] == detail, got.text   # 修前 201


def test_凭据作废原因全是空格422(client, admin):
    got = client.post("/api/credentials/99999999/void", headers=admin, json={"reason": "   "})
    assert got.status_code == 422 and got.json()["detail"] == "作废须填写原因", got.text   # 修前落到后面的 404


def test_整改提交完成说明全是空格422(client, admin, world):
    task = client.post("/api/performance/improvements", headers=admin, json={
        "org_id": world["org"], "problem": "P2309 随访率偏低", "owner_name": "张三", "due_date": "2030-01-01"})
    assert task.status_code == 201, task.text
    got = client.post(f"/api/performance/improvements/{task.json()['id']}/progress", headers=admin,
                      json={"complete": True, "completion_note": "   "})
    assert got.status_code == 422 and got.json()["detail"] == "提交完成须填写整改结果说明", got.text   # 修前 200
