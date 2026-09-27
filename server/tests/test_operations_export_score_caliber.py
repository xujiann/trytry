"""运营月报导出的绩效分与绩效页排名同一口径（P2-647，第十四批「导出 / 打印 vs 页面」扫描 R1-2）。

绩效页「机构评分排名」可以调两项计分参数（量类封顶次数、处方合格是否计系统自动通过），同一页上的「运营月报CSV」
里也有一列绩效评分——导出原先恒按缺省口径计分、表头也不说：页面按收紧的口径排出来的分，导出来又是另一套。

修法：导出收同名同义的两个参数，页面转发；不是缺省口径时表头写明，缺省口径的表头与原先一字不差。
"""
import csv
import io
from pathlib import Path

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Prescription, User

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def org(client, admin):
    org_id = client.post("/api/organizations", headers=admin, json={
        "name": "P2647 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2647 患者", "id_card": "330281197707072647"}).json()["id"]
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").one()
        # 两张系统自动通过、一张药师审过：计自动通过时合格率 100%，只计人工审核时 1/3
        for status in ("auto_passed", "auto_passed", "approved"):
            db.add(Prescription(patient_id=patient, org_id=org_id, diagnosis_name="P2647", status=status,
                                created_by=doctor.id))
        db.commit()
    return org_id


def _export(client, admin, query=""):
    resp = client.get(f"/api/reports/operations/export{query}", headers=admin)
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))
    return rows[0], {int(r[0]): float(r[-1]) for r in rows[1:]}


def _page(client, admin, query=""):
    body = client.get(f"/api/performance/orgs{query}", headers=admin).json()
    return body["period"], {c["org_id"]: c["score"] for c in body["scorecards"]}


def test_调过口径的导出与页面同分_表头写明(client, admin, org):
    query = "?volume_cap=2&include_auto_passed=false"
    header, exported = _export(client, admin, query)
    period, page = _page(client, admin, query)
    assert exported[org] == page[org], (exported[org], page[org])   # 修前导出恒按缺省口径
    assert exported[org] != _page(client, admin)[1][org]            # 两种口径在这家确实不同分，本条才有区分力
    assert header[-1] == f"绩效评分({period}，量类封顶2次，处方合格只计药师人工审核通过)"


def test_缺省口径的表头一字不差(client, admin, org):
    header, exported = _export(client, admin)
    period, page = _page(client, admin)
    assert header[-1] == f"绩效评分({period})" and period == str(clock.today().year)
    assert exported[org] == page[org]


def test_页面把两项计分参数转发给导出():
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    body = source[source.index("async function renderPerformance()"):]
    body = body[:body.index("\nasync function ", 1)]
    assert '["volume_cap", "include_auto_passed"].includes(k)' in body
    assert "/api/reports/operations/export${scoreQuery ? `?${scoreQuery}` : \"\"}" in body   # 修前不带参数
    assert "${scoreQuery ? `&${scoreQuery}` : \"\"}" in body
