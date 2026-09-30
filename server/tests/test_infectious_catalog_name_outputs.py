"""传染病：预警按目录印病种名，报告卡、法定上报 CSV、迟报清单却印报告人手填的写法（P2-943，第二十六批「导出 / 打印 /
下载 vs 页面」扫描 H1-4）。

P2-159 的修法写明「按编码分组，名称显示目录名，目录外的才取报告里的写法」，只修了多点预警；同一份数据的另外几个出口
没跟上。J11 报了三例（「流感」「甲流？」「流行性感冒」）：预警一行「流行性感冒 3 例」，供手工网报的法定导出里「病种名称」
一列却是「流感 / 甲流？/ 流行性感冒」。

修法：报告卡、法定导出、迟报清单的病种名称，目录内的印目录名、目录外的照旧取报告里的写法；导出的列不增不减
（有按位置读最后一列「及时性」的对接方），报告卡的字段也不增。手填写法在病例列表（报告原样的登记簿）里照看。
"""
import csv
import io

import pytest

from app.database import SessionLocal
from app.models import InfectiousCase

J11_WRITTEN = ("流感", "甲流？", "流行性感冒")


@pytest.fixture(scope="module")
def cases(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2943 报卡医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]

    def report(code, name, onset="2026-09-01"):
        resp = client.post("/api/infectious/cases", headers=admin, json={
            "org_id": org, "disease_code": code, "disease_name": name, "onset_date": onset})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    ids = {name: report("J11", name) for name in J11_WRITTEN}
    ids["outside"] = report("P2943X", "P2943 目录外新发病")
    return ids


def _csv(client, admin):
    resp = client.get("/api/infectious/cases/export.csv", headers=admin)
    assert resp.status_code == 200, resp.text
    return {int(r["卡片编号"]): r for r in csv.DictReader(io.StringIO(resp.text.lstrip("﻿")))}


def test_报告卡_目录内印目录名_目录外照旧(client, admin, cases):
    card = client.get(f"/api/infectious/cases/{cases['甲流？']}/report-card", headers=admin).json()
    assert card["disease_name"] == "流行性感冒"   # 修前「甲流？」
    outside = client.get(f"/api/infectious/cases/{cases['outside']}/report-card", headers=admin).json()
    assert outside["disease_name"] == "P2943 目录外新发病"


def test_法定导出_病种名称列一律目录名_列不增不减(client, admin, cases):
    rows = _csv(client, admin)
    assert {rows[cases[name]]["病种名称"] for name in J11_WRITTEN} == {"流行性感冒"}   # 修前三种写法
    assert rows[cases["outside"]]["病种名称"] == "P2943 目录外新发病"
    header = client.get("/api/infectious/cases/export.csv", headers=admin).text.lstrip("\ufeff").splitlines()[0]
    assert header.split(",")[-1] == "及时性"


def test_病例列表是登记簿_照旧显示填写名称(client, admin, cases):
    listed = {row["id"]: row["disease_name"] for row in client.get("/api/infectious/cases", headers=admin).json()}
    assert listed[cases["甲流？"]] == "甲流？"


def test_迟报清单印目录名(client, admin, cases):
    with SessionLocal() as db:   # 发病一个月后才报：迟报
        db.get(InfectiousCase, cases["流感"]).onset_date = "2026-08-01"
        db.commit()
    late = {row["case_id"]: row for row in client.get("/api/infectious/late-reports", headers=admin).json()}
    assert late[cases["流感"]]["disease_name"] == "流行性感冒"   # 修前「流感」
