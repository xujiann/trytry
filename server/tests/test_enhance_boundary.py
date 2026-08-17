"""S18 第十五轮边界用例：Hamilton 极端权重、预测样本分段、履约超额封顶、CDA 转义。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.allocation import hamilton_amounts, hamilton_cents
from app.main import app


# ---------------------------------------------------------------- 纯算法边界（无需 client）
def test_hamilton_边界精确到分():
    assert sum(hamilton_cents(100.0, [1, 1, 1])) == 10000       # 100 元 3 户均分，合计恰 100
    assert hamilton_amounts(100.0, [0, 0, 0]) == [0.0, 0.0, 0.0]  # 全零权重全 0
    assert hamilton_amounts(0.0, [1, 2, 3]) == [0.0, 0.0, 0.0]    # 总额 0
    assert hamilton_amounts(100.0, []) == []                      # 空
    neg = hamilton_amounts(100.0, [-5, 1])                        # 负权重按 0，另一户拿满
    assert neg == [0.0, 100.0]
    single = hamilton_amounts(33.33, [7])                         # 单户拿全额
    assert single == [33.33]
    # 除不尽也分毫不差
    assert round(sum(hamilton_amounts(100.0, [1, 1, 1])), 2) == 100.0


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


# ---------------------------------------------------------------- 预测样本分段边界
def test_forecast_样本不足与恰好三点(client, admin):
    # 空库：非零月 <3 → 样本不足
    r = client.get("/api/intel/forecast?metric=encounters&months=6", headers=admin).json()
    assert r["forecast"] == [] and r["note"] == "样本不足"
    # 造恰好 3 个非零月的就诊（直接落库回填 created_at）
    from datetime import datetime
    from app.database import SessionLocal
    from app.models import Encounter, Organization, Patient
    org = client.post("/api/organizations", json={"name": "边界院", "org_type": "township", "level": "township"}, headers=admin).json()
    pat = client.post("/api/patients", json={"name": "边界居民", "id_card": "320000199006061234"}, headers=admin).json()
    with SessionLocal() as db:
        for mon in (5, 6, 7):
            db.add(Encounter(patient_id=pat["id"], org_id=org["id"], doctor_name="医",
                             diagnosis_name="x", created_at=datetime(2026, mon, 15)))
        db.commit()
    r2 = client.get("/api/intel/forecast?metric=encounters&months=12&horizon=2", headers=admin).json()
    # 3 个非零点足以外推：forecast 非空
    assert len(r2["forecast"]) == 2, r2


# ---------------------------------------------------------------- 履约率超额封顶
def test_履约率超额封顶到1(client, admin):
    org = client.post("/api/organizations", json={"name": "封顶院", "org_type": "township", "level": "township"}, headers=admin).json()
    pat = client.post("/api/patients", json={"name": "封顶居民", "id_card": "320000199007071234"}, headers=admin).json()
    pkg = client.post("/api/service-packages", json={"code": "PKG-CAP", "name": "封顶包"}, headers=admin).json()
    it = client.post(f"/api/service-packages/{pkg['id']}/items", json={"service_type": "followup", "name": "随访", "annual_times": 2}, headers=admin).json()
    c = client.post("/api/contracts", json={"patient_id": pat["id"], "org_id": org["id"], "doctor_name": "医"}, headers=admin).json()
    client.patch(f"/api/contracts/{c['id']}/package", json={"package_id": pkg["id"]}, headers=admin)
    # 应履约 2，实际履约 3（超额）
    for _ in range(3):
        client.post(f"/api/contracts/{c['id']}/fulfill", json={"item_id": it["id"]}, headers=admin)
    f = client.get(f"/api/contracts/{c['id']}/fulfillment", headers=admin).json()
    assert f["expected_total"] == 2 and f["done_total"] == 3
    assert f["fulfillment_rate"] == 1.0  # 封顶，不虚增到 1.5


# ---------------------------------------------------------------- CDA 特殊字符转义
def test_cda_特殊字符转义(client, admin):
    from app.database import SessionLocal
    from app.models import Admission, Bed, Patient, Ward
    org = client.post("/api/organizations", json={"name": "转义院", "org_type": "lead_hospital", "level": "county"}, headers=admin).json()
    client.post("/api/users", json={"username": "b_op", "password": "pw123456", "full_name": "对接", "role": "operator", "org_id": org["id"]}, headers=admin)
    # 名字含 XML 敏感字符
    pat = client.post("/api/patients", json={"name": "张<&>三", "id_card": "320000199008081234"}, headers=admin).json()
    with SessionLocal() as db:
        w = Ward(org_id=org["id"], name="病区"); db.add(w); db.flush()
        bed = Bed(ward_id=w.id, bed_no="B1"); db.add(bed); db.flush()
        adm = Admission(patient_id=pat["id"], org_id=org["id"], ward_id=w.id, bed_id=bed.id,
                        doctor_name="医", diagnosis_name="d", status="admitted", created_by=1)
        db.add(adm); db.flush(); aid = adm.id
        db.commit()
    op = _login(client, "b_op", "pw123456")
    txt = client.get(f"/api/integration/cda/discharge/{aid}", headers=op).text
    assert "张&lt;&amp;&gt;三" in txt        # 已转义
    assert "张<&>三" not in txt              # 未原样注入
