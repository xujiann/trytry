"""性别取值没有闭集：存量导入的 1 / 2、M / F 原样落库，窗口更正能收「女性」；下游只认「男 / 女 / 未知」（P2-941，
第二十六批「人口学属性与业务对象的适配」扫描 H2-8）。

入站接口（HL7 / FHIR）与页面下拉都把性别收成「男 / 女 / 未知」，区域结构、审方、慢专病规则、FHIR 出站按这三个值判断；
建档请求模型只写了 `gender: str`、更正只校验出生日期、存量导入原样写入：导入 1 / 2 / F 三行，区域结构三人全算「未知」、
FHIR 出站 gender=unknown；存成「1」的男性有孕产档案时审方当孕产妇；更正成「女性」审批通过即落库。

修法：常见编码（GB/T 2261.1 的 0 / 1 / 2 / 9、HL7 的 M / F / U、FHIR 的 male / female、「男性 / 女性」）归一成三个值，
认不出的建档与更正 422、导入记错误行；出参不带归一（库里修之前存进去的照原样读出）。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import Patient

IDS = ['33010619800101011X', '330106198001010128', '330106198001010136', '330106198001010144',
       '330106198001010152', '330106198001010160', '330106198001010179']


@pytest.mark.parametrize(("written", "stored", "id_card"), [
    ("F", "女", IDS[0]), ("1", "男", IDS[1]), ("male", "男", IDS[2]), (" 女性 ", "女", IDS[3]), ("", "未知", IDS[4]),
], ids=["F", "1", "male", "女性", "空"])
def test_建档的性别编码归一成三个值(client, admin, written, stored, id_card):
    resp = client.post("/api/patients", headers=admin, json={
        "name": f"P2941 {written.strip() or '空'}", "id_card": id_card, "gender": written})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["gender"] == stored   # 修前原样落库


def test_认不出的性别_建档422(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "P2941 中性", "id_card": IDS[5], "gender": "中性"})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert "男 / 女 / 未知" in resp.text


def test_更正性别_认不出的422_编码写法归一后存(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2941 更正", "id_card": IDS[6], "gender": "男"}).json()["id"]
    bad = client.post("/api/consents/corrections", headers=admin, json={
        "patient_id": patient, "changes": {"gender": "中性"}, "reason": "登记有误"})
    assert bad.status_code == 422, bad.text   # 修前 201，审批通过即落库
    good = client.post("/api/consents/corrections", headers=admin, json={
        "patient_id": patient, "changes": {"gender": "女性"}, "reason": "登记有误"})
    assert good.status_code == 201, good.text
    assert good.json()["changes"] == '{"gender": "女"}'


def test_存量导入_编码归一_认不出的记错误行(tmp_path):
    reset_database()
    path = tmp_path / "patients.csv"
    path.write_text("name,id_card,gender\n"
                    "P2941 导入男,330102195001012941,1\n"
                    "P2941 导入女,330102195001022942,2\n"
                    "P2941 导入F,330102195001032943,F\n"
                    "P2941 导入X,330102195001042944,X\n", encoding="utf-8")
    rep = run_import("patients", path)
    assert rep.imported == 3, rep.errors
    assert [(line, "性别（X）认不出" in msg) for line, msg in rep.errors] == [(5, True)]   # 修前四行都原样导入
    with SessionLocal() as db:
        rows = db.query(Patient.name, Patient.gender).filter(Patient.name.like("P2941 导入%")).order_by(Patient.name)
        assert dict(rows.all()) == {"P2941 导入F": "女", "P2941 导入女": "女", "P2941 导入男": "男"}
