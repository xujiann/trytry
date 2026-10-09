"""病种编码只按原样查重：大小写、首尾空白不同就建出平行病种，同一患者能为同一病种建两份档案（P2-1742，第五十一批扫描 AO4-6）。

建病种（`chronic.create_disease_type`）原先只按原样查重（`get_disease_type` 用 `==`）。扫描实测（修前代码，`r1_catalog.py`）：
已有 `hypertension` 时，`Hypertension`、`hypertension ` 两种编码、名称都写「高血压」，建病种都 201，建档下拉里出现几个同名的
「高血压」；同一患者再按 `Hypertension` 建档 201，与 `hypertension` 档案并存（在管人数多算一份）。挂在变体编码上的档案也收不到
FHIR 入站的血压（`FIELD_DISEASE` 只认规范写法）。`texttypes.code_key` 已是编码比对的统一口径（P1-218 / P1-219）。

修法：建病种时按 `code_key` 与已有编码比，撞上 409 并点名已有的那条；字面完全相同的照旧原文案「病种编码已存在」。名称重名
不提示（`高血压` 这样的不同编码照收），存量不动；改病种不收 `code`，不涉及。
"""
import pytest

from app.database import SessionLocal
from app.models import ChronicDiseaseType, ChronicPatient

C = "/api/chronic"


def _detail(name: str, code: str, typed: str) -> str:
    return (f"已有病种「{name}」（编码 {code}），与填写的编码「{typed}」只差首尾空白、全半角、大小写或不可见字符，"
            "疑似同一病种：请沿用已有病种")


def _codes() -> list[str]:
    with SessionLocal() as db:
        return [code for (code,) in db.query(ChronicDiseaseType.code).order_by(ChronicDiseaseType.id)]


@pytest.mark.parametrize("typed", ["Hypertension", "hypertension ", "ＨＹＰＥＲＴＥＮＳＩＯＮ", "hypertension​"],
                         ids=["大小写", "尾随空格", "全角", "零宽空格"])
def test_写法不同的编码409_点名已有的那条(client, admin, typed):
    before = _codes()
    resp = client.post(f"{C}/disease-types", headers=admin, json={"code": typed, "name": "高血压", "level_rules": {}})
    assert resp.status_code == 409, resp.text   # 修前 201，建档下拉里多一个同名「高血压」
    assert resp.json() == {"detail": _detail("高血压", "hypertension", typed)}
    assert _codes() == before


def test_字面完全相同的照旧原文案(client, admin):
    resp = client.post(f"{C}/disease-types", headers=admin, json={"code": "hypertension", "name": "高血压"})
    assert resp.status_code == 409 and resp.json() == {"detail": "病种编码已存在"}, resp.text


def test_不同编码照收_自建病种同样按比对键挡(client, admin):
    same_name = client.post(f"{C}/disease-types", headers=admin, json={"code": "高血压", "name": "高血压"})
    assert same_name.status_code == 201, same_name.text   # 编码不同照收；名称重名不提示
    created = client.post(f"{C}/disease-types", headers=admin, json={"code": "p21742_ckd", "name": "P21742 慢性肾病"})
    assert created.status_code == 201, created.text
    resp = client.post(f"{C}/disease-types", headers=admin, json={"code": " P21742_CKD", "name": "P21742 肾病"})
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": _detail("P21742 慢性肾病", "p21742_ckd", " P21742_CKD")}
    other = client.post(f"{C}/disease-types", headers=admin, json={"code": "p21742_ckd2", "name": "P21742 慢性肾病二"})
    assert other.status_code == 201, other.text   # 只是前缀相同的不同编码照收


def test_同一患者不会因变体编码多出第二份档案(client, admin):
    """修前：`Hypertension` 病种 201，同一患者按它建档 201，与 `hypertension` 档案并存。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21742 慢病院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21742 患者", "id_card": "330106196501011742"}).json()["id"]
    first = client.post(C, headers=admin, json={"patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert first.status_code == 201, first.text
    assert client.post(f"{C}/disease-types", headers=admin,
                       json={"code": "Hypertension", "name": "高血压"}).status_code == 409   # 修前 201
    second = client.post(C, headers=admin, json={"patient_id": patient, "disease": "Hypertension", "managed_by_org_id": org})
    assert second.status_code == 422 and second.json() == {"detail": "病种编码不在慢病病种目录内"}, second.text   # 修前 201
    with SessionLocal() as db:
        files = db.query(ChronicPatient.disease).filter(ChronicPatient.patient_id == patient).all()
    assert [disease for (disease,) in files] == ["hypertension"]
