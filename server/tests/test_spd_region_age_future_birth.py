"""卫健端区域结构分析自己算年龄、没跟上 P2-713：出生日期在将来的人算进「0-17」（P2-939，第二十六批「人口学属性与
业务对象的适配」扫描 H2-6）。

P2-713 定了「将来的出生日期按写坏处理、当不知道」，规则事实（`service._age_of`）与审方（`prescriptions._age_of`）都照做；
区域结构（`/api/spd/stats/region`）在循环里自己算年龄，负数落进 `<18` 那一档。HL7 A04 带 PID-7=20700101 建档（入站照收，
P1-61 剩余部分）后纳管：区域结构「0-17」计 1 人、「未知」0 人，同一人在规则事实里 age=None。

修法：分档改用 `service._age_of`，与规则事实同一个算法。
"""
import pytest

from app.database import SessionLocal
from app.models import Patient

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2939 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = client.post("/api/patients", headers=admin, json={
        "name": "P2939 入站居民", "id_card": "330106197505050073", "gender": "男", "birth_date": "1975-05-05"})
    assert made.status_code in (200, 201), made.text
    with SessionLocal() as db:   # 入站 / P2-713 之前存下的将来出生日期（接口建档已拦，这里直接落库）
        db.get(Patient, made.json()["id"]).birth_date = "2070-01-01"
        db.commit()
    return {"org": org, "patient": made.json()["id"]}


def _ages(client, admin):
    return client.get(f"{B}/stats/region", headers=admin, params={"program_code": "hypertension"}).json()[
        "age_distribution"]


def test_将来的出生日期计入未知_不进0到17(client, admin, world):
    before = _ages(client, admin)
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "org_id": world["org"]})
    assert enrolled.status_code == 201, enrolled.text
    after = _ages(client, admin)
    assert after["未知"] == before["未知"] + 1   # 修前不变
    assert after["0-17"] == before["0-17"]       # 修前 +1
