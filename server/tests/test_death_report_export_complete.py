"""死因报告卡导出不再被静默截断在 2000 条（P1-50）。

`GET /api/certs/death-report-cards/export.csv` 供手工网报人口死亡信息登记管理系统或交换前置机对接，
是**法定上报口径**。原实现 `.limit(2000)` 取死亡证明再拼 CSV：日期区间内死亡数超过 2000 时，导出少掉
一部分，调用方无从知道——法定报表少报。去掉上限要同时拿掉随后那句 `Patient.id.in_([...])`
（参数个数有上限），改成死亡证明外连接患者一次取回；每张挂了患者的卡照旧各落一条调阅留痕。

两段分工：特征化用例在改前改后都绿（逐列内容与留痕），灌量用例造 2100 张卡钉「修了什么」。
"""
import csv
import io

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import AccessLog, MedicalCert, Organization, Patient, User

CAP = 2000  # 原硬编码上限


def _rows(client, admin, **params):
    r = client.get("/api/certs/death-report-cards/export.csv", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))


@pytest.fixture(scope="module")
def small(client, admin):
    """两张 2031 年 1 月的死亡证明：一张挂了患者，一张没挂（外院转来的只有姓名）。"""
    with SessionLocal() as db:
        org = Organization(name="死因导出卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        patient = Patient(ehc_no="EHC-DR-1", name="死因导出患者", id_card="330166194001011234",
                          gender="男", birth_date="1940-01-01")
        db.add(patient)
        db.flush()
        creator = db.query(User).filter(User.username == "admin").one()
        db.add_all([
            MedicalCert(cert_type="death", cert_no="DR-S-1", name="死因导出患者", event_date="2031-01-05",
                        detail="急性心肌梗死", org_id=org.id, patient_id=patient.id,
                        created_by=creator.id),
            MedicalCert(cert_type="death", cert_no="DR-S-2", name="无档案死者", event_date="2031-01-06",
                        detail="脑出血", org_id=org.id, created_by=creator.id),
        ])
        db.commit()
        return {"org_id": org.id, "patient_id": patient.id, "creator": creator.id}


def _logs(patient_id):
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.patient_id == patient_id,
                                          AccessLog.resource == "death_report_card").count()


def test_特征化_逐列内容与留痕(client, admin, small):
    before = _logs(small["patient_id"])
    rows = _rows(client, admin, date_from="2031-01-01", date_to="2031-01-31")
    assert rows[0] == ["证明编号", "姓名", "性别", "身份证号", "出生日期", "死亡日期", "死因诊断",
                       "签发机构", "签发人", "签发时间"]
    body = rows[1:]
    assert [r[0] for r in body] == ["DR-S-1", "DR-S-2"]
    assert body[0][1] == "死因导出患者" and body[0][5] == "2031-01-05" and body[0][6] == "急性心肌梗死"
    assert body[0][7] == "死因导出卫生院" and body[0][8] == "admin"
    assert body[1][1] == "无档案死者" and body[1][6] == "脑出血"
    assert _logs(small["patient_id"]) == before + 1, "挂了患者的卡每导出一次落一条调阅留痕"


@pytest.fixture(scope="module")
def bulk(small):
    """2031 年 3 月再灌 `CAP + 100` 张没挂患者的死亡证明。"""
    with SessionLocal() as db:
        db.execute(insert(MedicalCert), [
            {"cert_type": "death", "cert_no": f"DR-B-{i:05d}", "name": f"灌量死者{i}", "event_date": "2031-03-10",
             "detail": "肺炎", "org_id": small["org_id"], "created_by": small["creator"]}
            for i in range(CAP + 100)
        ])
        db.commit()
    return {"n": CAP + 100}


def test_不再被上限截断(client, admin, bulk):
    rows = _rows(client, admin, date_from="2031-03-01", date_to="2031-03-31")
    assert len(rows) - 1 == bulk["n"], "法定上报导出被截断在上限上了"
