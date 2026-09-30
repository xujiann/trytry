"""比对键认得出夹着看不见的格式字符的同一个编码 / 诊断：零宽空格 U+200B、BOM U+FEFF、词连接符 U+2060 不再让审方规则、
禁忌诊断、接种禁忌失效（P2-1145，第三十三批扫描 A3-1）。

`texttypes.code_key` / `text_key` 原先只做 NFKC、去空白、大小写归一；Unicode 类别 Cf 的字符 NFKC 不动，`strip()` /
`split()` 也不当空白。开方、建规则、登记禁忌的编码都是手输框，从网页、微信、富文本编辑器复制出来的文字常带这类字符，
肉眼看不出来。2026-09-30 开发库实测（种子规则 B01AA03 华法林上限 10mg）：
- 编码写成 `B01AA03`+U+200B、U+FEFF+`B01AA03`、`B01AA03`+U+2060，各开 50mg，全部 201 auto_passed（原样写法转药师审）；
- 规则编码建成 `A3ZW-DIG`+U+200B（上限 0.25mg），按 `A3ZW-DIG` 开 1mg → auto_passed；
- 诊断写成 `QT`+U+200B+`间期延长`，命中不了阿奇霉素的禁忌「QT间期延长」→ auto_passed；
- 禁忌登记成 `HepB`+U+200B（过敏性休克），接种前评估 allowed=True、接种登记 201。

修法：两个比对键共用一步「先去掉 Cf 类字符」，再做原有的 NFKC 等归一；只用于比对，不改落库的值。
"""
import pytest

from conftest import login

from app.texttypes import code_key, has_keyword, text_key

ZWSP, BOM, WJ, SHY, LRM = "\u200b", "\ufeff", "\u2060", "\u00ad", "\u200e"
WARFARIN, DIGOXIN, AZI = "P21145W01", "P21145D01", "P21145AZ1"
VACCINE = "P21145HepB"


def test_比对键_去掉格式字符():
    for variant in ("B01AA03" + ZWSP, BOM + "B01AA03", "B01AA03" + WJ, "B01" + SHY + "AA03", LRM + " b01aa03 "):
        assert code_key(variant) == code_key("B01AA03") == "B01AA03", ascii(variant)   # 修前多出一个不可见字符
    assert text_key("QT" + ZWSP + "间期延长") == text_key("QT间期延长") == "qt间期延长"
    assert text_key(BOM + "高血压" + WJ) == text_key("高血压")
    assert has_keyword("社区获得性肺炎；QT" + ZWSP + "间期延长", ["QT间期延长"])
    assert code_key(ZWSP) == text_key(BOM + WJ) == ""   # 只有格式字符的，归一后为空（关键词不算命中）
    assert not has_keyword("高血压", [ZWSP])


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21145 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21145_doc", "password": "passw0rd1", "full_name": "P21145 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21145 患者", "id_card": "330102197001011145"}).json()["id"]
    for body in ({"drug_code": WARFARIN, "max_daily_dose": 10, "dose_unit": "mg"},
                 # 反方向：药师建规则时编码从网页复制带出零宽，处方按正常写法开
                 {"drug_code": DIGOXIN + ZWSP, "max_daily_dose": 0.25, "dose_unit": "mg"},
                 {"drug_code": AZI, "max_daily_dose": 500, "dose_unit": "mg", "contraindicated_diagnoses": "QT间期延长"}):
        created = client.post("/api/prescriptions/rules", headers=admin, json=body)
        assert created.status_code == 201, created.text
    batch = client.post("/api/vaccine-supply/batches", headers=admin, json={
        "vaccine_code": VACCINE, "vaccine_name": "乙肝疫苗", "batch_no": "P21145-B001", "expire_date": "2099-12-31",
        "org_id": org, "quantity": 20})
    assert batch.status_code == 201, batch.text
    return {"org": org, "patient": patient, "batch": batch.json()["id"],
            "doctor": login(client, "p21145_doc", "passw0rd1")}


def _rx(client, world, code, dose, diagnosis="心房颤动"):
    resp = client.post("/api/prescriptions", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": diagnosis,
        "items": [{"drug_code": code, "drug_name": "P21145 药品", "daily_dose": dose}]})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("code", [WARFARIN + ZWSP, BOM + WARFARIN, WARFARIN + WJ],
                         ids=["尾随零宽空格", "前导BOM", "尾随词连接符"])
def test_开方编码夹格式字符_照样按规则审_超量转药师(client, world, code):
    rx = _rx(client, world, code, 50)
    assert rx["status"] == "pending_review" and "超过上限" in rx["review_comment"], rx   # 修前 auto_passed


def test_规则编码夹格式字符_按正常写法开方照样审(client, world):
    rx = _rx(client, world, DIGOXIN, 1)
    assert rx["status"] == "pending_review" and "超过上限" in rx["review_comment"], rx   # 修前 auto_passed


def test_诊断夹格式字符_禁忌诊断照样判(client, world):
    rx = _rx(client, world, AZI, 500, diagnosis="社区获得性肺炎；QT" + ZWSP + "间期延长")
    assert rx["status"] == "pending_review" and "禁忌诊断" in rx["review_comment"], rx   # 修前 auto_passed


def test_接种禁忌编码夹格式字符_照样拦(client, admin, world):
    kid = client.post("/api/patients", headers=admin, json={
        "name": "P21145 受种者", "id_card": "110101202301011145", "birth_date": "2023-01-01"}).json()["id"]
    contra = client.post("/api/vaccination/contraindications", headers=admin, json={
        "patient_id": kid, "vaccine_code": VACCINE + ZWSP, "reason": "上一剂后过敏性休克", "contra_type": "permanent"})
    assert contra.status_code == 201, contra.text
    pre = client.get("/api/vaccination/pre-check", headers=admin,
                     params={"patient_id": kid, "vaccine_code": VACCINE}).json()
    assert pre["allowed"] is False and pre["contraindications"], pre   # 修前 True []
    resp = client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": kid, "vaccine_code": VACCINE, "vaccine_name": "乙肝疫苗", "dose_no": 1, "org_id": world["org"],
        "batch_id": world["batch"]})
    assert resp.status_code == 409 and "存在接种禁忌" in resp.json()["detail"], resp.text   # 修前 201


def test_原样编码照旧_规则外的药照旧系统审通过(client, world):
    assert _rx(client, world, WARFARIN, 3)["status"] == "auto_passed"
    assert _rx(client, world, "P21145-NORULE" + ZWSP, 300)["status"] == "auto_passed"
