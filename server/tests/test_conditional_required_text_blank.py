"""「这种情况下须填写」的文字只判 `not x`：一串空格照收（P2-309）。

四处条件必填的文字（字段本身带默认空串，只在某种情况下必填）判的是 `if not body.x`——空串挡住了，一串空格却当成填了：
- AEFI 上报未关联接种记录时「须填写疫苗编码」：存进一串空格，按疫苗统计归不了类；
- 死亡医学证明「须填写死因诊断」、出生缺陷登记「须填写缺陷诊断」：证明上的诊断是空白；
- 就诊凭据作废「须填写原因」：作废留痕的原因是空白；
- 绩效整改提交完成「须填写整改结果说明」：待确认队列里的说明是空白；
- 专病中途退出「须填写原因」（P2-418，第七批扫描补读到）：出组留痕的退出原因是空白；
- 处方点评「不合理须注明问题类型或点评意见」（P2-1662，第四十九批扫描 AM3-4）：判的是 `not (issues or comment)`，两项只填
  空格 / 全角空格 / 零宽字符照收（201），点评结论「不合理」却没写问题，之后想更正又是 409「该处方已点评」。
与 `texttypes.NON_BLANK`（必填文本不收纯空白，P1-109）同一个口径，只是这几处按情况必填、没法写在字段声明上。

修法：判 `strip()` 之后的；处方点评那一处判 `texttypes.is_blank_text`（与 NON_BLANK 同一个判据，零宽字符也算没填）。
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


def test_专病中途退出原因全是空格422(client, admin, world):
    from app.database import SessionLocal
    from app.models import DiseaseEnrollment, DiseaseProgram

    with SessionLocal() as db:
        program = DiseaseProgram(code="p2418_prog", name="P2418 专病", org_id=world["org"],
                                 path_nodes=[{"key": "n1", "name": "首诊"}], active=True)
        db.add(program)
        db.flush()
        enrollment = DiseaseEnrollment(program_id=program.id, patient_id=world["patient"], org_id=world["org"],
                                       status="enrolled")
        db.add(enrollment)
        db.commit()
        enrollment_id = enrollment.id
    got = client.post(f"/api/disease-programs/enrollments/{enrollment_id}/exit", headers=admin,
                      json={"status": "exited", "exit_reason": "   "})
    assert got.status_code == 422 and got.json()["detail"] == "中途退出须填写原因", got.text   # 修前 200
    assert client.post(f"/api/disease-programs/enrollments/{enrollment_id}/exit", headers=admin,
                       json={"status": "exited", "exit_reason": "转院"}).status_code == 200


@pytest.mark.parametrize("blank", ["   ", "　　", "​"], ids=["空格", "全角空格", "零宽字符"])
def test_处方点评不合理_问题类型与点评意见都是看不见的字422_不落库(client, admin, world, blank):
    from app.database import SessionLocal
    from app.models import PrescriptionComment

    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": "上呼吸道感染",
        "items": [{"drug_code": "P1662-AMB", "drug_name": "P1662 氨溴索", "daily_dose": 60, "days": 3}]})
    assert rx.status_code == 201, rx.text
    rx_id = rx.json()["id"]
    got = client.post(f"/api/prescriptions/{rx_id}/comment-review", headers=admin,
                      json={"grade": "unreasonable", "issues": blank, "comment": blank})
    assert got.status_code == 422 and got.json()["detail"] == "不合理处方须注明问题类型或点评意见", got.text   # 修前 201
    with SessionLocal() as db:
        assert db.query(PrescriptionComment).filter(PrescriptionComment.prescription_id == rx_id).count() == 0
    # 正常文字照收（写一项就够）——修前空白那条已经落库，这里拿到的是 409「该处方已点评」
    ok = client.post(f"/api/prescriptions/{rx_id}/comment-review", headers=admin,
                     json={"grade": "unreasonable", "issues": blank, "comment": "疗程偏长"})
    assert ok.status_code == 201, ok.text
