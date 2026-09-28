"""传染病报告卡判不了及时性的两种情形分开说（P2-686，第十六批「导出 / 打印 vs 页面」扫描线索）。

`late` 为 null 有两种：目录外病种——没有法定时限；发病日期非法（存量里的坏日期，入参早已校验）——法定时限照给
（目录里有），只是算不出迟没迟。原先报告卡页面一律写「无法定时限」，发病日期非法那张卡上同时印着「法定时限 24 小时」，
自相矛盾；报告卡导出的「及时性」一栏两种都留空，与页面对不上。修后页面与导出同一套说法：「无法定时限」/「无法判定」。
"""
import csv
import io
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import InfectiousCase

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def cases(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2686 报卡医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]

    def report(code, name):
        resp = client.post("/api/infectious/cases", headers=admin, json={
            "org_id": org, "disease_code": code, "disease_name": name, "onset_date": "2026-09-01"})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    outside = report("P2686X", "P2686 目录外病种")
    bad_onset = report("A15", "肺结核")
    with SessionLocal() as db:   # 存量里的坏发病日期：入参早已校验，只能直接落库造
        db.get(InfectiousCase, bad_onset).onset_date = "2026-02-30"
        db.commit()
    return {"outside": outside, "bad_onset": bad_onset}


def test_报告卡_目录外无时限_坏发病日期有时限但判不了(client, admin, cases):
    outside = client.get(f"/api/infectious/cases/{cases['outside']}/report-card", headers=admin).json()
    bad = client.get(f"/api/infectious/cases/{cases['bad_onset']}/report-card", headers=admin).json()
    assert (outside["report_hours"], outside["late"]) == (None, None)
    assert (bad["report_hours"], bad["late"]) == (24, None)   # 时限照给——页面据此不能写「无法定时限」


def test_导出的及时性与页面同一套说法(client, admin, cases):
    resp = client.get("/api/infectious/cases/export.csv", headers=admin)
    assert resp.status_code == 200, resp.text
    rows = {int(r["卡片编号"]): r for r in csv.DictReader(io.StringIO(resp.text.lstrip("﻿")))}
    assert rows[cases["outside"]]["及时性"] == "无法定时限"   # 修前留空
    assert (rows[cases["bad_onset"]]["法定时限(小时)"], rows[cases["bad_onset"]]["及时性"]) == ("24", "无法判定")


def test_页面按有没有时限分开写():
    src = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    assert 'undetermined: ["无法判定", ""]' in src
    assert 'c.late === null ? (c.report_hours === null ? "unknown" : "undetermined")' in src   # 修前一律 unknown
