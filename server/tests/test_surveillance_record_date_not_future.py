"""症候群和病原日报的日期能填将来：报的数不进今天的多点预警，等到那一天又冒出一条「预警」（P2-1433，第四十二批扫描 AF2-2）。

`SyndromeIn.record_date` / `PathogenIn.record_date` 只校验格式，写入时不跟今天比。实测（修前）：今天 10-04，把腹泻 9 例
（阈值 3）的日期敲成 10-14，201、回执 `alert: true`——多点预警面板只截到今天，里面没有这一条；到 10-14 它以「当天 9 例」
进预警，那天机构真报时又被当成原上报覆盖（见 P2-1432）。病原同样：30 检 15 阳敲成 10-14 照收。同一条预警链上的传染病
报卡早就拦了将来的发病日期（P2-454：敲错月份就落在预警窗口外、预警少一例），接种日期也拦了（P2-1304）。

修法：两个接口日期晚于今天的 422「日期（…）不得晚于今天」，写法与业务日（`clock.today()`）照传染病报卡。
"""
from datetime import date

import pytest
from conftest import freeze_business_date

B = "/api/surveillance"
TODAY = date(2026, 10, 4)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21433 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]


@pytest.mark.parametrize("path, body", [
    ("syndromes", {"syndrome": "diarrhea", "case_count": 9, "threshold": 3}),
    ("pathogens", {"pathogen_name": "诺如病毒", "tested_count": 30, "positive_count": 15}),
])
def test_日报日期_明天422不落库_今天201(client, admin, org, path, body):
    with freeze_business_date(TODAY):
        future = client.post(f"{B}/{path}", headers=admin, json={"org_id": org, **body, "record_date": "2026-10-05"})
        assert future.status_code == 422, future.text   # 修前 201（症候群回执还写着 alert: true）
        assert future.json()["detail"] == "日期（2026-10-05）不得晚于今天"
        today = client.post(f"{B}/{path}", headers=admin, json={"org_id": org, **body, "record_date": "2026-10-04"})
        assert today.status_code == 201, today.text
    rows = client.get(f"{B}/{path}", headers=admin, params={"org_id": org}).json()
    assert [r["record_date"] for r in rows] == ["2026-10-04"]   # 将来那一条没落库


def test_今天报的照进今天的多点预警(client, admin, org):
    with freeze_business_date(TODAY):
        alerts = client.get(f"{B}/alerts", headers=admin).json()
    assert alerts["window"]["end"] == "2026-10-04"
    assert [(a["case_count"], a["threshold"]) for a in alerts["syndrome_alerts"] if a["org_id"] == org] == [(9, 3)]
    assert [(a["tested_count"], a["positive_count"]) for a in alerts["pathogen_alerts"] if a["org_id"] == org] == [(30, 15)]
