"""数据质控只扫每张表的前 5000 行：县域患者、就诊动辄数万，后面的违规永远查不出（P1-115）。

每条规则取 `limit(SCAN_LIMIT)`（5000）行来判，没有 ORDER BY：开发库按插入序取前 5000 行，生产库按堆序取任意
5000 行；`/run` 与 `/summary` 却把这 5000 行里查出的数当全量报出。原注释说「超出部分下次整改后再扫」——整改只改
值不删行，前 5000 行永远是那 5000 行，后面的一行都轮不到。

2026-09-25 实测（修前代码）：5000 条合规就诊之后插一条诊断编码为空的，`/run?rule_code=QC004` 报 0 条违规。

修法：按主键分批把整张表扫完（一批仍是 SCAN_LIMIT 行，内存上限不变），顺序按主键、两库一致。
"""
import pytest
from sqlalchemy import insert

N_COMPLIANT = 5000


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Encounter

    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1115 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P1115 就诊人", "id_card": "330192198001011234"}).json()
    code = client.get("/api/dictionaries/diagnosis/entries?q=", headers=admin).json()[0]["code"]
    rows = [{"patient_id": patient["id"], "org_id": org["id"], "diagnosis_code": code, "diagnosis_name": "合规"}
            for _ in range(N_COMPLIANT)]
    with SessionLocal() as db:
        db.execute(insert(Encounter), rows)
        late = Encounter(patient_id=patient["id"], org_id=org["id"], diagnosis_code="", diagnosis_name="缺编码")
        db.add(late)
        db.commit()
        return {"late": late.id}


def test_第5001条就诊的违规也查得出(client, admin, world):
    resp = client.get("/api/dataquality/run", headers=admin, params={"rule_code": "QC004", "limit": 1000})
    assert resp.status_code == 200, resp.text
    hits = {v["record_id"] for v in resp.json()["items"]}
    assert world["late"] in hits, resp.json()["total"]   # 修前：第 5001 行扫不到，报 0 条


def test_汇总按全表计数(client, admin, world):
    resp = client.get("/api/dataquality/summary", headers=admin)
    assert resp.status_code == 200, resp.text
    qc004 = next(r for r in resp.json()["by_rule"] if r["rule_code"] == "QC004")
    assert qc004["violations"] >= 1   # 修前 0


def test_分批扫描不漏不重_按主键升序(world, monkeypatch):
    """批大小调到 7：5001 行要翻 700 多批，逐行核对不漏不重、按主键升序（不依赖库的物理顺序）。"""
    from app.database import SessionLocal
    from app.models import Encounter
    from app.routers import dataquality

    monkeypatch.setattr(dataquality, "SCAN_LIMIT", 7)
    with SessionLocal() as db:
        expected = [i for (i,) in db.query(Encounter.id).order_by(Encounter.id)]
        seen = [row.id for row in dataquality._scan(db.query(Encounter), Encounter)]
    assert seen == expected and len(seen) == len(set(seen)) >= N_COMPLIANT + 1
