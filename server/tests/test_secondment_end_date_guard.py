"""派驻「结束」两条端点的日期入参与员工状态回写（ADR-0024 第一步）。

## 防的是哪两件事

**一、非法日期能写进 `secondments.end_date`，那条派驻随即从国家监测指标里消失。**
两条 `end` 端点的 `end_date` 都是**查询参数**，而 D-3 那一轮日期治理只换了 **body 字段**
（`SecondmentCreate.start_date` / `SecondmentIn.*` 都换成了 `DateStr`），查询参数不在射程内。
于是 `?end_date=完全不是日期` 一律 200 落库；`staffing.dispatch_stats` 对日期解析失败的
记录**整条跳过**，一名真派满半年的中级医师就此从「中级及以上医师派驻 6 个月以上人数」
里消失。`staffing` 那条看着有校验，其实只有一句 `finish < row.start_date` ——
**那是字典序比较，不是日期校验**：`2026-13-99` / `abc` / `2026-02-31` 的字典序都大于
开始日，照样 200 落库（ADR-0024 实测二）。

**二、`mgmt` 的结束端点会把已离职的员工改回在岗。**
它的 `employee.status = "active"` 是**无条件**的，而 `staffing` 那条有
`status == "seconded"` 前置条件。于是「派驻中 → 登记离职 → 经 mgmt 结束派驻」
会把 `status` 从 `left` 改回 `active`，而 `analytics` 的在岗医师数正按
`status == "active"` 计数（ADR-0024 第四处分叉，此前无人登记）。

## 第三件：别让下一处又留在裸 `str`

月度那头有 `datetypes.PeriodStr`（body）+ `deps.require_month`（查询参数）一对，
并有 `test_periodstr_single_source.py` 盯着；日期这头一直**只有 body 那一半**。
现在补上了 `deps.require_date`，并立了一条按「日期查询参数是否经过守卫」推导欠账的
棘轮，名单只许变少（P1-58）。那条棘轮起初写在本文件末尾，P1-58 开工时迁到了
`test_date_query_params.py`——它管的是全平台的日期查询参数，不只是派驻。
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def sec_base(client, admin):
    """一个派出院 + 一个接收院 + 两名员工（各自独立，不蹭别的模块的数据）。"""
    org1 = client.post(
        "/api/organizations",
        json={"name": "派驻日期守卫总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "派驻日期守卫卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    return {"org1": org1, "org2": org2}


def _employee(client, admin, sec_base, name):
    return client.post(
        "/api/mgmt/employees",
        json={"org_id": sec_base["org1"]["id"], "name": name, "title": "主治医师",
              "position": "医师"},
        headers=admin,
    ).json()["id"]


def _secondment(client, admin, sec_base, employee_id, start="2026-01-10"):
    resp = client.post(
        "/api/staffing/secondments",
        json={"employee_id": employee_id, "from_org_id": sec_base["org1"]["id"],
              "to_org_id": sec_base["org2"]["id"], "start_date": start,
              "assignment_type": "long_term", "position": "内科"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


#: 四个都是**字典序大于开始日**的非法值——正是 `staffing` 那句比较放行、
#: 而 `dispatch_stats` 会整条跳过的那一类。`2026-02-31` 形状合法但日历上不存在。
BAD_DATES = ["完全不是日期", "2026-13-99", "abc", "2026-02-31"]


# ---------------------------------------------------------------- 一、两条 end 都挡


@pytest.mark.parametrize("bad", BAD_DATES)
def test_mgmt结束派驻拒绝非法日期(client, admin, sec_base, bad):
    sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, f"甲{bad}"))
    resp = client.post(f"/api/mgmt/secondments/{sid}/end?end_date={bad}", headers=admin)
    assert resp.status_code == 422, resp.text
    assert "end_date" in resp.json()["detail"]
    # 被拒之后**没有半途落库**：那条派驻仍在派
    row = client.get(f"/api/staffing/secondments?to_org_id={sec_base['org2']['id']}",
                     headers=admin).json()
    assert [r for r in row if r["id"] == sid][0]["ongoing"] is True


@pytest.mark.parametrize("bad", BAD_DATES)
def test_staffing结束派驻同样拒绝非法日期(client, admin, sec_base, bad):
    """这一条才是关键：它看着"有校验"，而那句比较挡不住字典序更大的非法值。"""
    sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, f"乙{bad}"))
    resp = client.post(f"/api/staffing/secondments/{sid}/end?end_date={bad}", headers=admin)
    assert resp.status_code == 422, resp.text
    assert "end_date" in resp.json()["detail"]


def test_合法日期照常结束(client, admin, sec_base):
    sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, "丙合法"))
    resp = client.post(f"/api/mgmt/secondments/{sid}/end?end_date=2026-03-01", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": sid, "end_date": "2026-03-01"}


def test_mgmt也校验结束日不早于开始日(client, admin, sec_base):
    """此前只有 staffing 有这一道；两条路对同一个动作给两种答案。"""
    sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, "丁倒挂"))
    resp = client.post(f"/api/mgmt/secondments/{sid}/end?end_date=2026-01-01", headers=admin)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "结束日期不得早于开始日期"}


def test_staffing留空仍取业务日期_行为未变(client, admin, sec_base):
    """`?end_date=` 与不带这个参数都应回落到业务今天——补校验不得把这条既有路径挡掉。"""
    for query in ("", "?end_date="):
        sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, f"戊{query}"))
        resp = client.post(f"/api/staffing/secondments/{sid}/end{query}", headers=admin)
        assert resp.status_code == 200, (query, resp.text)
        assert resp.json()["ongoing"] is False
        assert resp.json()["end_date"], query  # 取到了今天，不是空串


# ---------------------------------------------------------------- 二、离职不得被改回在岗


def test_已离职员工不因结束派驻被改回在岗(client, admin, sec_base):
    """顺序：建派驻（status=seconded）→ 登记离职（status=left）→ 经 mgmt 结束派驻。

    修复前 `mgmt` 无条件写 `active`，这名离职员工会重新出现在
    `analytics` 的在岗医师数里；`staffing` 那条因为有前置条件，一直是对的。
    """
    emp = _employee(client, admin, sec_base, "己离职")
    sid = _secondment(client, admin, sec_base, emp)

    leave = client.post(
        f"/api/mgmt/employees/{emp}/changes",
        json={"change_type": "leave", "effective_date": "2026-02-01"},
        headers=admin,
    )
    assert leave.status_code == 201, leave.text
    assert leave.json()["employee_status"] == "left"

    ended = client.post(f"/api/mgmt/secondments/{sid}/end?end_date=2026-02-10", headers=admin)
    assert ended.status_code == 200, ended.text

    row = [e for e in client.get("/api/mgmt/employees", headers=admin).json() if e["id"] == emp]
    assert row and row[0]["status"] == "left", "结束派驻把已离职的员工改回了在岗"


def test_在派员工结束派驻后回到在岗(client, admin, sec_base):
    """反向：正常路径不能因为加了条件而失灵。"""
    emp = _employee(client, admin, sec_base, "庚在派")
    sid = _secondment(client, admin, sec_base, emp)
    assert client.post(
        f"/api/mgmt/secondments/{sid}/end?end_date=2026-02-10", headers=admin
    ).status_code == 200
    row = [e for e in client.get("/api/mgmt/employees", headers=admin).json() if e["id"] == emp]
    assert row and row[0]["status"] == "active"


# ---------------------------------------------------------------- 三、指标不再被一条请求污染


def test_非法日期再也进不了下沉指标(client, admin, sec_base):
    """ADR-0024 实测一那条路径的回归：写不进去，`invalid_date_records` 就不会涨。"""
    before = client.get("/api/staffing/dispatch-stats", headers=admin).json()["invalid_date_records"]
    sid = _secondment(client, admin, sec_base, _employee(client, admin, sec_base, "辛指标"))
    for path in (f"/api/mgmt/secondments/{sid}/end", f"/api/staffing/secondments/{sid}/end"):
        assert client.post(f"{path}?end_date=完全不是日期", headers=admin).status_code == 422
    after = client.get("/api/staffing/dispatch-stats", headers=admin).json()["invalid_date_records"]
    assert after == before

