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
现在补上了 `deps.require_date`，本文件末尾那条棘轮按「日期查询参数是否经过
`require_date`」推导欠账，名单只许变少（P1-58）。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"


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


# ---------------------------------------------------------------- 四、棘轮：只许变少


#: 还留在裸 `str`、未经 `require_date` 的日期查询参数（P1-58）。**只许变少。**
#: 判据是推导出来的（下面 `_unguarded_date_params` 扫路由函数签名），不是手工清单：
#: 新增一个不走 `require_date` 的日期查询参数会让 `test_不得新增裸日期查询参数` 变红。
#: 这 25 处未逐条判定过——多数是只读筛选（危害远小于本案那两条**写进日期列**的），
#: `billing.run_reconciliation` 则是就地 `strptime` + 422 的第三种写法。
KNOWN_BARE_DATE_PARAMS: set[str] = {
    "routers/admin_mgmt.py::list_rosters::duty_date",
    "routers/appointments.py::find_doctors::from_date",
    "routers/appointments.py::list_slots::slot_date",
    "routers/billing.py::list_reconciliation::date",
    "routers/billing.py::run_reconciliation::date",
    "routers/certs.py::export_death_report_cards_csv::date_from",
    "routers/certs.py::export_death_report_cards_csv::date_to",
    "routers/clinical_docs.py::list_handovers::handover_date",
    "routers/medwaste.py::handler_stats::end_date",
    "routers/medwaste.py::handler_stats::start_date",
    "routers/portal.py::portal_slots::slot_date",
    "routers/resources.py::match_slots::from_date",
    "routers/surgery.py::list_schedules::scheduled_date",
    "spd/routers/care.py::list_case_reports::date_from",
    "spd/routers/care.py::list_case_reports::date_to",
    "spd/routers/care.py::list_revisits::date_from",
    "spd/routers/care.py::list_revisits::date_to",
    "spd/routers/followup.py::followup_stats::date_from",
    "spd/routers/followup.py::followup_stats::date_to",
    "spd/routers/followup.py::list_call_tasks::date_from",
    "spd/routers/followup.py::list_call_tasks::date_to",
    "spd/routers/followup.py::list_followup_records::date_from",
    "spd/routers/followup.py::list_followup_records::date_to",
    "spd/routers/referral.py::closure_rate::date_from",
    "spd/routers/referral.py::closure_rate::date_to",
}

_HTTP_VERBS = ("get", "post", "put", "patch", "delete")


def _route_functions():
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Attribute)
                    and d.func.attr in _HTTP_VERBS
                    for d in node.decorator_list
                ):
                    yield path, node


def _guarded_names(func) -> set[str]:
    """函数体里 `require_date(<名字>, ...)` 保护过的参数名。"""
    names = set()
    for sub in ast.walk(func):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "require_date"
        ):
            names.update(a.id for a in sub.args if isinstance(a, ast.Name))
    return names


def _unguarded_date_params() -> set[str]:
    out = set()
    for path, func in _route_functions():
        guarded = _guarded_names(func)
        for arg in list(func.args.args) + list(func.args.kwonlyargs):
            if "date" not in arg.arg or arg.annotation is None:
                continue
            if ast.unparse(arg.annotation) not in ("str", "str | None"):
                continue
            if arg.arg in guarded:
                continue
            rel = path.relative_to(APP_DIR).as_posix()
            out.add(f"{rel}::{func.name}::{arg.arg}")
    return out


def test_覆盖面自证():
    funcs = list(_route_functions())
    print(
        f"\n[日期查询参数棘轮] 扫描 {len(funcs)} 个路由函数；"
        f"未经 require_date 的日期参数 {len(_unguarded_date_params())} 处"
    )
    assert len(funcs) >= 500, f"只数到 {len(funcs)} 个路由函数，扫描面可能不对"


def test_不得新增裸日期查询参数():
    new = sorted(_unguarded_date_params() - KNOWN_BARE_DATE_PARAMS)
    assert new == [], (
        "以下日期查询参数没走 deps.require_date——裸 `str` 只是个字符串，"
        "`2026-02-31` / `完全不是日期` 会原样入库或进入筛选条件：\n  "
        + "\n  ".join(new)
        + "\n\nbody 字段用 datetypes.DateStr / OptionalDateStr，查询参数用 deps.require_date。"
    )


def test_名单只许变少():
    """接通一个就从名单里划掉；不划掉也红（否则名单会永远停在今天的数字）。"""
    stale = sorted(KNOWN_BARE_DATE_PARAMS - _unguarded_date_params())
    assert stale == [], (
        "这些已经走上 require_date（或已不存在）了，请从 KNOWN_BARE_DATE_PARAMS 划掉：\n  "
        + "\n  ".join(stale)
    )


def test_两条派驻结束端点已经不在名单里():
    """本批修的就是这两条——它们**必须**已经脱离欠账，否则这份用例在空转。"""
    unguarded = _unguarded_date_params()
    for entry in (
        "routers/admin_mgmt.py::end_secondment::end_date",
        "routers/staffing.py::end_secondment::end_date",
    ):
        assert entry not in unguarded, entry
        assert entry not in KNOWN_BARE_DATE_PARAMS, entry
