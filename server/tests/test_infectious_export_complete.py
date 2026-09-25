"""传染病报告卡导出不再被静默截断在 2000 条（P1-113）。

`GET /api/infectious/cases/export.csv` 与死因报告卡导出（P1-50）是同一类东西：供手工网报中国疾病预防控制信息系统
（大疫情网）或交换前置机对接的**法定上报口径**。P1-50 只修了死因那一份，这一份还是 `.limit(2000)`，而且按编号
**正序**取——超过 2000 张卡时，被截掉的是**最新**的那些（最需要补报的）；「只导迟报」又是在截断之后才筛，新近的
迟报一张都进不了清单，调用方无从知道。

修法：去掉上限。病种目录与机构名本就整表取进字典，没有 P1-50 那句 `IN` 的参数个数问题，不必改取数结构。
"""
import csv
import io
from datetime import datetime

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import InfectiousCase, Organization

CAP = 2000  # 原硬编码上限
ORG = "传染病导出卫生院"


@pytest.fixture(scope="module")
def bulk(client):
    """`CAP + 100` 张肺结核报告卡（法定 24 小时）：最早的都当天报，编号最大的 50 张隔了 30 天才报（迟报）。"""
    with SessionLocal() as db:
        org = Organization(name=ORG, org_type="township", level="township")
        db.add(org)
        db.flush()
        rows = []
        for i in range(CAP + 100):
            at = datetime(2031, 1, 31 if i >= CAP + 50 else 1, 10, 0)
            rows.append({"org_id": org.id, "disease_code": "A15", "disease_name": "肺结核", "category": "B",
                         "onset_date": "2031-01-01", "reported_at": at, "created_at": at})
        db.execute(insert(InfectiousCase), rows)
        db.commit()
    return {"n": CAP + 100, "late": 50}


def _rows(client, admin, **params) -> list[list[str]]:
    r = client.get("/api/infectious/cases/export.csv", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return [row for row in csv.reader(io.StringIO(r.text.lstrip("﻿")))][1:]


def test_超过2000张照样全导出(client, admin, bulk):
    body = [r for r in _rows(client, admin, disease_code="A15") if r[1] == ORG]
    assert len(body) == bulk["n"]   # 修前 2000：最新的 100 张不见了


def test_只导迟报不漏新近的迟报(client, admin, bulk):
    body = [r for r in _rows(client, admin, disease_code="A15", late_only="true") if r[1] == ORG]
    assert len(body) == bulk["late"]   # 修前 0：截断留下的是最早的 2000 张，全是当天报的
    assert {r[-1] for r in body} == {"迟报"}
