"""居民端 H5 的文案与后端对得上：病种名取自目录、同意方式用后端给的名字、类别编码与后端一致（P2-210）。

H5 自带的几张对照表与后端各说各的：病种只认三个（目录里另五个种子病种印成 chd / stroke 之类的英文编码）；
同意书的采集方式写「窗口代提」而后端（也是纸面打印）是「窗口代录」；更正申请「已驳回」与模型注释「已拒绝」不一致；
费用类别写成 treat / material，后端从来是 treatment，治疗处置费印成英文；慢专病宣教推送（spd_edu）没有中文名。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static", "m")


def _m_js() -> str:
    with open(os.path.join(STATIC, "m.js"), encoding="utf-8") as fh:
        return fh.read()


def test_H5不再自带病种与同意方式的对照表():
    source = _m_js()
    assert "CHRONIC_NAMES" not in source and "CONSENT_METHOD_NAMES" not in source      # 修前各有一张
    assert 'esc(c.disease_name || c.disease)' in source
    assert 'esc(c.method_name || c.method)' in source
    assert "窗口代提" not in source
    status_map = source[source.index("const CORRECTION_STATUS = {"):]
    assert 'rejected: ["已拒绝", "red"]' in status_map[:status_map.index("};")]            # 修前「已驳回」


def test_费用类别键与后端收费目录一致_宣教推送有中文名():
    from app.routers.billing import CHARGE_CATEGORY_NAMES

    source = _m_js()
    line = source[source.index("const CATEGORY_TEXT = {"):]
    line = line[:line.index("};")]
    keys = {part.split(":")[0].strip() for part in line[len("const CATEGORY_TEXT = {"):].split(",") if ":" in part}
    assert keys == set(CHARGE_CATEGORY_NAMES)                                              # 修前有 treat / material、没有 treatment
    assert 'spd_edu: "健康宣教"' in source


@pytest.fixture(scope="module")
def resident(client, admin):
    from app.database import SessionLocal
    from app.models import SmsCode

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2210 居民", "id_card": "330106197112121626", "phone": "13700022101"}).json()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2210 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient["id"], "disease": "chd", "managed_by_org_id": org})
    assert resp.status_code in (200, 201), resp.text
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == "13700022101").delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": "13700022101", "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": "13700022101", "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_档案里的病种名取自目录(client, resident):
    care = client.get("/api/portal/me/archive", headers=resident).json()["chronic_care"]
    assert [(c["disease"], c["disease_name"]) for c in care] == [("chd", "冠心病")]
