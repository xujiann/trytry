"""慢专病按诊断编码识别：国临版扩展码同样给出四位亚目（P2-1077，第三十一批「编码体系与术语」扫描 C4-1）。

`platform.diagnosis_codes` 的 docstring 写「父目一并给出，否则病种规则要把亚目列全」，却只补三位类目（`split(".")[0]`）：
规则按四位亚目写的（糖尿病肾病 `E11.2`、阵发性房颤 `I48.0`），碰到国临版 2.0 的 `E11.201`、`I48.000` 永不命中——
`in [E11.2]` 对 E11.201 为假，`in [E11]` 对 E11.201 却为真。修后亚目一并给出；小数点后是占位 `x` 的（`I10.x00`）没有
亚目，只给类目。各病种纳入码集本身（冠心病缺 I24、脑卒中缺 I69 等）另行登记，不在这里定。
"""
import pytest

from app.database import SessionLocal
from app.spd.platform import diagnosis_codes
from app.spd.rules import evaluate


@pytest.fixture(scope="module")
def patient(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21077 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    pid = client.post("/api/patients", headers=admin, json={
        "name": "P21077 居民", "id_card": "330106196002021077"}).json()["id"]
    for code in ("E11.201", "I10.x00", "I48.000"):
        resp = client.post("/api/encounters", headers=admin, json={
            "patient_id": pid, "org_id": org, "diagnosis_code": code, "diagnosis_name": "P21077"})
        assert resp.status_code in (200, 201), resp.text
    return pid


def test_国临版扩展码给出亚目与类目_占位x只给类目(patient):
    with SessionLocal() as db:
        codes = diagnosis_codes(db, patient)
    assert set(codes) == {"E11.201", "E11.2", "E11", "I10.x00", "I10", "I48.000", "I48.0", "I48"}   # 修前没有 E11.2、I48.0
    assert "I10.x" not in codes


@pytest.mark.parametrize("rule_codes", [["E11.2"], ["I48.0"], ["E11"]])
def test_按亚目或类目写的规则都命中国临版编码(patient, rule_codes):
    with SessionLocal() as db:
        facts = {"diagnosis": diagnosis_codes(db, patient)}
    hit, _ = evaluate([{"field": "diagnosis", "op": "in", "value": rule_codes}], facts)
    assert hit   # 修前 E11.2、I48.0 不命中
