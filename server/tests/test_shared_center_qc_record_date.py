"""共享中心质控记录没填日期的按登记那天补，页面质控表出日期列（P2-1596，第四十七批扫描 AK1-9）。

修前：`admin_mgmt.add_qc` 的 `record_date` 缺省空串、原样落库；「行政与质控」页的质控表只有中心、项目、结果、备注四列。
扫描实测（`r3_oa.py`）：一条「检验中心 生化室内质控 不合格」不填日期登记 201，回执与清单里 `record_date` 都是空串，页面上
看不出是哪天的不合格。

修法：没填日期的按登记那天的本地业务日补（「没填日期按录入那天算」是平台已有口径，P2-660；取日期走 `resolve_business_date`，
即 `clock.today()`，同文件排班清单、合同到期同一个入口），写了日期的照存；页面质控表加「日期」列（`esc`）。登记人要加列迁移，
不在本条。用例按仓库钉时钟的写法（`freeze_business_date`）把业务日冻住再比。
"""
from datetime import date
from pathlib import Path

from conftest import freeze_business_date

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_没填日期_按登记那天的本地业务日补(client, admin):
    with freeze_business_date(date(2031, 3, 1)):
        for extra in ({}, {"record_date": ""}):   # 不送、送空串都算没填
            resp = client.post("/api/mgmt/qc", headers=admin, json={
                "center_type": "lab", "item": "P21596 生化室内质控", "result": "fail", **extra})
            assert resp.status_code == 201, resp.text
            assert resp.json()["record_date"] == "2031-03-01", extra   # 修前 ""
            listed = [q for q in client.get("/api/mgmt/qc", headers=admin).json() if q["id"] == resp.json()["id"]]
            assert [q["record_date"] for q in listed] == ["2031-03-01"]


def test_写了日期的照存(client, admin):
    with freeze_business_date(date(2031, 3, 1)):
        resp = client.post("/api/mgmt/qc", headers=admin, json={
            "center_type": "imaging", "item": "P21596 CT 水模", "result": "pass", "record_date": "2031-02-20"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["record_date"] == "2031-02-20"


def test_页面质控表有日期列_经esc():
    start = PAGE.index("async function renderOaQc()")
    body = PAGE[start:PAGE.index("\n}\n", start)]
    assert 'table(["日期", "中心", "项目", "结果", "备注"], qc,' in body   # 修前只有后四列
    assert '`<tr><td>${esc(q.record_date) || "—"}</td>' in body
