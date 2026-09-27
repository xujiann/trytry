"""打印件正文里的时刻按本地时间印，与页脚「打印时间」同一把尺子（P2-535，第十批「日期与期间边界」扫描 X3-4）。

页脚「打印时间」取 `now_local()`；正文的开方 / 签发 / 报告 / 入出院时间原先直接 strftime 落库的 naive UTC——
东八区部署下同一张纸上正文比页脚早 8 小时，早上 7 点半入院的出院小结印成前一天夜里 23:30。
临床文书的 `_shown_time`（P2-455）早就换成本地再显示，打印这边没跟上。
"""
import re
import time
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, User, Ward


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def admission(client):
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2535 县医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        ward = Ward(org_id=org.id, name="P2535 内科")
        patient = Patient(ehc_no="EHC-P2535", name="P2535 患者", id_card="330106198808082535")
        db.add_all([ward, patient])
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="P2535")
        db.add(bed)
        db.flush()
        # 本地 09-27 07:30 入院（落库 UTC 09-26 23:30）、本地 09-29 17:00 出院（UTC 09-29 09:00）
        adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="discharged",
                        admitted_at=datetime(2026, 9, 26, 23, 30), discharged_at=datetime(2026, 9, 29, 9, 0),
                        created_by=admin_id)
        db.add(adm)
        db.commit()
        return adm.id


def test_出院小结的入出院时间按本地印_与页脚同一把尺子(client, admin, admission, east_eight):
    html = client.get(f"/api/print/discharge-summaries/{admission}", headers=admin).text
    # 修前：入院时间印 2026-09-26 23:30、出院时间 2026-09-29 09:00
    assert '<td class="k">入院时间</td><td>2026-09-27 07:30</td>' in html
    assert '<td class="k">出院时间</td><td>2026-09-29 17:00</td>' in html
    printed = re.search(r"打印时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", html)
    assert printed, html
    footer = datetime.strptime(printed.group(1), "%Y-%m-%d %H:%M:%S")
    assert abs((datetime.now() - footer).total_seconds()) < 120   # 页脚本来就是本地时刻


def test_落库时刻换本地的_helper(east_eight):
    from app.clock import to_local

    assert to_local(datetime(2026, 9, 26, 23, 30)) == datetime(2026, 9, 27, 7, 30)
