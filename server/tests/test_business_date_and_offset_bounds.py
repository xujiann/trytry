"""业务日期、分页偏移、急救节点时间取到极端值，整个请求 500（P2-410）。

P1-96 给「天数」入参补了上界（≤ 3650），日期本身却没有范围：`?today=` 与起止日期类查询参数走
`deps.resolve_business_date`，只校验 `YYYY-MM-DD` 形状与日历，公元 1 年、9999 年都收——预警 / 到期类接口拿它
加减天数，贴着两头的一加减就越出 `date` 的表示范围（OverflowError），老年预警、医废滞留、合同 / 中药批次 / 知识库 /
药品批次到期、传染病与症候群多点预警、号源匹配九个接口实测 500。分页的 `offset` 没有上界，天文数字交给驱动，
SQLite 抛 OverflowError、PostgreSQL 报「bigint out of range」，所有分页清单与驾驶舱下钻 500。急救节点时间带时区
偏移时要换算成本地钟点（`_wall_clock`），「0001-01-01T00:00+14:00」一换算就越界，记节点 500；慢专病按方案生成随访的
基准日（请求体）加上方案时间点同样越界；号源批量生成逐日展开区间，止日是 9999-12-31 时最后一步加出界。

修后：业务日期限在 1900-01-01 ~ 2999-12-31（422 并说明范围）；offset 钳到 64 位上限（越过总数照旧回空页）；
节点时间在校验时就试换算、越界 422；随访基准日与业务日期同一个范围；号源展开先判是否到止日再加一天。
"""
from datetime import date

import pytest
from pydantic import ValidationError

from app.deps import BUSINESS_DATE_MAX, BUSINESS_DATE_MIN, MAX_OFFSET, clamp_offset

EXTREME = [
    ("/api/eldercare/alerts?today={d}", "0001-01-01", "today"),
    ("/api/medwaste/alerts?today={d}", "0001-01-01", "today"),
    ("/api/mgmt/staff-contracts/expiring?days=3650&today={d}", "9999-12-31", "today"),
    ("/api/tcm/preparation-batches/expiring?days=3650&today={d}", "9999-12-31", "today"),
    ("/api/knowledge/expiring?days=3650&today={d}", "9999-12-31", "today"),
    ("/api/infectious/alerts?window_days=3650&today={d}", "0001-01-01", "today"),
    ("/api/pharmacy/batches/expiring?days=3650&today={d}", "9999-12-31", "today"),
    ("/api/surveillance/alerts?days=90&today={d}", "0001-01-01", "today"),
    ("/api/resources/match/slots?days=90&from_date={d}", "9999-12-31", "from_date"),
]


@pytest.mark.parametrize("url, extreme, field", EXTREME)
def test_业务日期贴着公元1年或9999年_422说明范围而不是500(client, admin, url, extreme, field):
    got = client.get(url.format(d=extreme), headers=admin)
    assert (got.status_code, got.json()["detail"]) == (
        422, f"{field} 参数须在 1900-01-01 ~ 2999-12-31 之间"), got.text   # 修前 500：OverflowError


@pytest.mark.parametrize("url, extreme, field", EXTREME)
def test_范围两端照常(client, admin, url, extreme, field):
    for edge in (BUSINESS_DATE_MIN, BUSINESS_DATE_MAX):
        got = client.get(url.format(d=edge.isoformat()), headers=admin)
        assert got.status_code == 200, (edge, got.text)


def test_日期格式不对的文案不变(client, admin):
    got = client.get("/api/eldercare/alerts?today=2026-02-30", headers=admin)
    assert (got.status_code, got.json()["detail"]) == (422, "today 参数须为 YYYY-MM-DD 格式")


def test_分页偏移取到天文数字_回空页而不是500(client, admin):
    for url in ("/api/patients", "/api/homevisits"):
        got = client.get(url, headers=admin, params={"offset": 2**70})
        assert got.status_code == 200 and got.json() == [], got.text   # 修前 500
        assert "x-total-count" in got.headers
    got = client.get("/api/metrics/drilldown", headers=admin, params={"metric": "referrals_up", "offset": 2**70})
    assert got.status_code == 200 and got.json()["items"] == [], got.text   # 驾驶舱下钻手写的那份同样钳住


def test_偏移钳到64位上限_负数照旧按0():
    assert (clamp_offset(-5), clamp_offset(0), clamp_offset(30)) == (0, 0, 30)
    assert clamp_offset(2**70) == MAX_OFFSET == 2**63 - 1   # PostgreSQL 的 bigint 恰好收到这个数，再大就报错


@pytest.fixture(scope="module")
def case_id(client, admin):
    return client.post("/api/emergency/cases", headers=admin, json={"location": "P2410 村口"}).json()["id"]


@pytest.mark.parametrize("occurred_at", ["0001-01-01T00:00+14:00", "9999-12-31T23:59-14:00"])
def test_急救节点时间换算越界_422而不是500(client, admin, case_id, occurred_at):
    got = client.post(f"/api/emergency/cases/{case_id}/milestones", headers=admin,
                      json={"milestone": "onset", "occurred_at": occurred_at})
    assert got.status_code == 422 and "超出可换算的时间范围" in got.text, got.text   # 修前 500


def test_急救节点带时区偏移的正常时间照收(client, admin, case_id):
    got = client.post(f"/api/emergency/cases/{case_id}/milestones", headers=admin,
                      json={"milestone": "onset", "occurred_at": "2026-08-10T14:02+08:00"})
    assert got.status_code == 201, got.text


def test_随访计划的基准日与业务日期同一个范围():
    from app.spd.routers.followup import GeneratePlanIn

    with pytest.raises(ValidationError, match="base_date 须在 1900-01-01 ~ 2999-12-31 之间"):
        GeneratePlanIn(patient_id=1, rule_id=1, base_date="9999-12-31")   # 修前照收，加上时间点就 500
    assert GeneratePlanIn(patient_id=1, rule_id=1, base_date="2999-12-31").base_date == "2999-12-31"
    assert GeneratePlanIn(patient_id=1, rule_id=1).base_date == ""       # 缺省取今天，照旧
    assert BUSINESS_DATE_MIN == date(1900, 1, 1) and BUSINESS_DATE_MAX == date(2999, 12, 31)


def test_号源批量生成区间止于9999年末_照常生成而不是500(client, admin):
    """逐日展开区间是「加一天、再判是否过了止日」：止日是 9999-12-31 时最后那一步加出界（P2-410）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2410 号源院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    got = client.post("/api/appointments/slots/batch", headers=admin, json={
        "org_id": org, "date_from": "9999-12-30", "date_to": "9999-12-31",
        "templates": [{"resource_type": "outpatient", "resource_name": "P2410 内科"}]})
    assert (got.status_code, got.json()) == (201, {"created": 2, "skipped": 0}), got.text   # 修前 500
