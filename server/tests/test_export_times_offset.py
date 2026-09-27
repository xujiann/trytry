"""离开平台的时刻都带上时区（P2-536，第十批「日期与期间边界」扫描 X3-3）。

`clock.py`：「需要给外部（接口返回、导出）标注时区的，用 `to_aware()` 在出口处转一次」——`to_aware` 却一处都没人调：
- FHIR 批量导出的 `Encounter.period.start`、`DiagnosticReport.issued` 写的是不带时区的落库值，而 FHIR 规定 dateTime
  带到时分就必须带时区（instant 同）；同一批导出的 manifest 倒是 `now_aware()`；
- HL7 ACK 的 MSH-7 取 UTC、不写偏移——HL7 v2 的时间戳不带偏移就按发送方本地时间解读；
- 死因 / 传染病报告卡 CSV 是拿去手工网报的，签发 / 报告时间写的是 naive UTC，誊录的人当成北京时间，差 8 小时。

修后 FHIR 与 MSH-7 标 UTC 偏移，两份 CSV 写带偏移的本地时间（`clock.local_iso`）；页面用的 JSON 卡片不动（显示侧口径见 P1-105）。
"""
import csv
import io
import re
import time
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Encounter, ExamReport, InfectiousCase, MedicalCert, Organization, User


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _csv(resp):
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))


def test_FHIR_的时刻带时区():
    from app.routers.integration import fhir_diagnostic_report_resource, fhir_encounter_resource

    moment = datetime(2026, 9, 26, 23, 30)
    encounter = Encounter(id=1, encounter_type="outpatient", org_id=1, created_at=moment,
                          doctor_name="", diagnosis_code="", diagnosis_name="")
    report = ExamReport(id=1, reported_at=moment, conclusion="", finding="", critical=False)
    # 修前两处都是 2026-09-26T23:30:00，读的一方只能猜是哪个时区
    assert fhir_encounter_resource(encounter, "EHC1")["period"]["start"] == "2026-09-26T23:30:00+00:00"
    assert fhir_diagnostic_report_resource(report, 1, "EHC1")["issued"] == "2026-09-26T23:30:00+00:00"


def test_HL7_应答的_MSH7_带偏移():
    from app.routers.integration import _build_ack

    msh7 = _build_ack("CTRL1").split("\r")[0].split("|")[6]
    assert re.fullmatch(r"\d{14}\+0000", msh7), msh7   # 修前只有 14 位数字


def test_两份报告卡_CSV_写带偏移的本地时间(client, admin, east_eight):
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2536 县医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        # 东八区 2031-05-02 07:30 签发 / 报告的（落库 UTC 2031-05-01 23:30）
        db.add(MedicalCert(cert_type="death", cert_no="DR-P2536", name="P2536 死者", event_date="2031-05-01",
                           detail="脑出血", org_id=org.id, created_by=admin_id, created_at=datetime(2031, 5, 1, 23, 30)))
        case = InfectiousCase(org_id=org.id, disease_code="P2536", disease_name="P2536 病种", onset_date="2031-05-01",
                              reported_at=datetime(2031, 5, 1, 23, 30))
        db.add(case)
        db.commit()
        case_id = case.id

    death = _csv(client.get("/api/certs/death-report-cards/export.csv", headers=admin,
                            params={"date_from": "2031-05-01", "date_to": "2031-05-01"}))
    assert [r["签发时间"] for r in death if r["证明编号"] == "DR-P2536"] == ["2031-05-02T07:30:00+08:00"]   # 修前 2031-05-01T23:30:00
    cases = _csv(client.get("/api/infectious/cases/export.csv", headers=admin, params={"disease_code": "P2536"}))
    assert [r["报告时间"] for r in cases if r["卡片编号"] == str(case_id)] == ["2031-05-02T07:30:00+08:00"]


def test_带偏移的本地_ISO_helper(east_eight):
    from app.clock import local_iso

    assert local_iso(datetime(2026, 9, 26, 23, 30, 5, 123456)) == "2026-09-27T07:30:05+08:00"
