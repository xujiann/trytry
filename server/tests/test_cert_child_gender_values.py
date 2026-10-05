"""法定医学证明与儿保档案的性别与建档同一口径（P2-1540，第四十五批「门诊西药发药与法定医学证明」扫描 AI3-9）。

修前 `certs.CertCreate.gender` 与 `maternal.ChildCreate.gender` 是自由文本（只限 8 字），建档却早已走 `schemas._gender_input`
（`normalize_gender`，P2-941）：同一时刻建档填 F 归成「女」、X1 回 422，死亡证明填 F、1、女性、X1 全部 201，死因报告卡导出
CSV 的性别列原样是「F, 1, 女性, X1」（女性患者导成「1」，而 GB/T 2261.1 里 1 是男）；儿保档案填 M 照存（scan45 ai3
`r6_gender.py`）。

修法：两处入参复用同一个 `_gender_input`——常见编码归一成「男 / 女 / 未知」、认不出的 422、缺省照旧「未知」；出参不带归一：
儿保档案出参 `ChildOut` 继承自 `ChildCreate`，覆盖回不带校验器的 `gender: str`；证明清单与死因报告卡的出参本就另写
`gender: str`。库里修之前存进去的怪值照原样读出。
"""
import csv
import io

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import ChildRecord, MedicalCert, User

DAY = "2026-09-30"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21540 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    made = client.post("/api/users", headers=admin, json={
        "username": "p21540_dir", "password": "passw0rd1", "role": "director", "org_id": org, "full_name": "P21540 院长"})
    assert made.status_code in (200, 201), made.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21540 逝者", "id_card": "330106194501011540", "gender": "女", "birth_date": "1945-01-01"})
    assert patient.status_code in (200, 201), patient.text
    return {"org": org, "patient": patient.json()["id"], "director": login(client, "p21540_dir", "passw0rd1")}


def _death(client, admin, world, **extra):
    return client.post("/api/certs", headers=admin, json={
        "cert_type": "death", "name": "P21540 逝者", "event_date": DAY, "detail": "肺癌", "org_id": world["org"],
        "patient_id": world["patient"], **extra})


def test_证明的性别编码归一_认不出的422_死因报告卡导出印归一后的值(client, admin, world):
    issued = {}
    for written in ("F", "1", " 女性 ", None):   # None：不传，缺省「未知」
        resp = _death(client, admin, world, **({} if written is None else {"gender": written}))
        assert resp.status_code == 201, resp.text
        issued[resp.json()["cert_no"]] = written
    bad = _death(client, admin, world, gender="X1")
    assert bad.status_code == 422, bad.text   # 修前 201
    assert "性别（X1）只能是 男 / 女 / 未知" in bad.text
    listed = {c["cert_no"]: c["gender"] for c in client.get("/api/certs?cert_type=death", headers=admin).json()}
    assert {issued[no]: listed[no] for no in issued} == {"F": "女", "1": "男", " 女性 ": "女", None: "未知"}   # 修前原样
    export = client.get(f"/api/certs/death-report-cards/export.csv?date_from={DAY}&date_to={DAY}",
                        headers=world["director"])
    assert export.status_code == 200, export.text
    rows = list(csv.reader(io.StringIO(export.text.lstrip("﻿"))))
    assert rows[0][2] == "性别"
    assert {issued[r[0]]: r[2] for r in rows[1:]} == {"F": "女", "1": "男", " 女性 ": "女", None: "未知"}


def test_儿保档案的性别编码归一_认不出的422_缺省未知(client, admin):
    for written, stored in (("M", "男"), ("2", "女"), ("female", "女")):
        resp = client.post("/api/maternal/children", headers=admin, json={
            "name": f"P21540 婴儿{written}", "gender": written, "birth_date": "2026-09-01"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["gender"] == stored   # 修前原样落库（M 照存）
    bad = client.post("/api/maternal/children", headers=admin, json={
        "name": "P21540 婴儿X1", "gender": "X1", "birth_date": "2026-09-01"})
    assert bad.status_code == 422, bad.text
    plain = client.post("/api/maternal/children", headers=admin, json={"name": "P21540 婴儿缺省", "birth_date": "2026-09-01"})
    assert plain.status_code == 201 and plain.json()["gender"] == "未知", plain.text


def test_存量的怪值照原样读出(client, admin, world):
    """修之前存进去的「X1」「M」：儿保档案清单 200、原样读出（出参不带归一，否则 X1 让整张清单 500、M 被悄悄改写）；
    证明清单同理。"""
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        db.add_all([ChildRecord(name="P21540 存量X1", gender="X1", birth_date="2026-08-01"),
                    ChildRecord(name="P21540 存量M", gender="M", birth_date="2026-08-02"),
                    MedicalCert(cert_type="birth", cert_no="B-P21540-LEGACY", name="P21540 存量证明", gender="F",
                                event_date="2026-08-03", org_id=world["org"], created_by=creator)])
        db.commit()
    children = client.get("/api/maternal/children", headers=admin)
    assert children.status_code == 200, children.text[:200]
    got = {c["name"]: c["gender"] for c in children.json()}
    assert (got["P21540 存量X1"], got["P21540 存量M"]) == ("X1", "M")
    certs = {c["cert_no"]: c["gender"] for c in client.get("/api/certs?cert_type=birth", headers=admin).json()}
    assert certs["B-P21540-LEGACY"] == "F"
