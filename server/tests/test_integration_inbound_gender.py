"""HL7 PID-8 与 FHIR gender 入站走同一个 `normalize_gender`（P2-1078，第三十一批「编码体系与术语」扫描 C4-14）。

建档、更正、存量导入的性别早按 P2-941 收成「男 / 女 / 未知」三值，常见编码写法（GB/T 2261.1 的 1 / 2、HL7 的 M / F、
FHIR 的 male / female、「男性 / 女性」）归一过来；入站两个解析器却各用一张两项映射表：PID-8 送 1 / 2、m、「男 / 女」，
FHIR 送 Male、FEMALE，一律记成「未知」——而 A08 更新不拿「未知」覆盖已知性别，之后也改不回来。修后两处都调
`normalize_gender`，认不出的照旧落「未知」。
"""
import pytest

from app.database import SessionLocal
from app.models import Patient
from app.routers.integration import ID_CARD_SYSTEM


def _gender(resp) -> str:
    with SessionLocal() as db:
        return db.get(Patient, resp.json()["patient"]["id"]).gender


@pytest.mark.parametrize(("pid8", "expected", "n"), [
    ("1", "男", 1), ("2", "女", 2), ("m", "男", 3), ("女", "女", 4), ("F", "女", 5), ("X", "未知", 6)])
def test_PID8各种写法都认得(client, admin, pid8, expected, n):
    id_card = f"33028119800101{n:03d}8"
    message = "\r".join([
        f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260930090000||ADT^A04|P21078-{n}|P|2.4",
        f"PID|1||{id_card}^^^CN^ID||P21078 患者{n}||19800101|{pid8}",
    ])
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    assert _gender(resp) == expected   # 修前 1 / 2 / m / 女 都是「未知」


@pytest.mark.parametrize(("gender", "expected", "n"), [("Male", "男", 1), ("FEMALE", "女", 2), ("unknown", "未知", 3)])
def test_FHIR的gender不分大小写(client, admin, gender, expected, n):
    id_card = f"33028119810101{n:03d}9"
    resp = client.post("/api/integration/fhir/Patient", headers=admin, json={
        "resourceType": "Patient", "identifier": [{"system": ID_CARD_SYSTEM, "value": id_card}],
        "name": [{"text": f"P21078 FHIR{n}"}], "gender": gender})
    assert resp.status_code == 201, resp.text
    assert _gender(resp) == expected   # 修前 Male / FEMALE 是「未知」
