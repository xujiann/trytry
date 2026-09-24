"""日期查询参数只有一个校验真源：`datetypes.check_date`（P1-58）。

body 字段走 `DateStr` / `OptionalDateStr`，查询参数走 `deps.require_date`——两者都落到
`check_date`。本文件守查询参数这一半，并记录一处**校验随解释器升级悄悄变松**的实例。

## `resolve_business_date` 曾是第三套日期校验

41 个查询参数（38 个 `today`，外加 `from_date` ×2、`until`、`start`、`end`）经它校验，
它自己用 `date.fromisoformat`。这个函数从 Python 3.11 起放宽成接受 ISO 8601 的各种变体：
`20260901`、`2026-W36-2`、`2026W362` 都照过，而接口规范（docs/接口对接规范.md）写的是
`YYYY-MM-DD`。**没有任何一行代码改动，校验就变松了。**

后果不止是口径不一。`analytics.patient_flow` 拿解析后的日期筛县内就诊、拿**原串**筛
县外就诊；`?start=20260901` 时县外那一侧按字符串比较（`"2026-09-10" < "20260901"`）
整段落空，县外就诊从 1 变 0，县域就诊率虚高——修复前实测如此。

另一处小毛病：借它校验的 `from_date` / `until` / `start` / `end` 报错一律写着
"today 参数须为 YYYY-MM-DD 格式"，调用方看不出错的是哪个参数。
"""
from __future__ import annotations

import ast
import pathlib
from datetime import date

import pytest
from fastapi import HTTPException

from app import clock, deps

#: Python 3.11+ 的 `date.fromisoformat` 接受、而接口规范不接受的写法。
ISO_VARIANTS = ["20260901", "2026-W36-2", "2026W362"]


# ---------------------------------------------------------------- 一、resolve_business_date


@pytest.mark.parametrize("value", ISO_VARIANTS)
def test_resolve_business_date不再接受ISO变体(value):
    assert date.fromisoformat(value) == date(2026, 9, 1), "前提：解释器确实接受这些写法"
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date(value)
    assert exc.value.status_code == 422
    assert exc.value.detail == "today 参数须为 YYYY-MM-DD 格式", "today 的文案一字未改"


@pytest.mark.parametrize("value", ["", "abc", "2026-02-31", "2026-9-1", "2026-09-01\n"])
def test_resolve_business_date原本就拒的照样拒(value):
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date(value)
    assert exc.value.status_code == 422


def test_resolve_business_date合法值与缺省_行为未变():
    assert deps.resolve_business_date("2026-09-01") == date(2026, 9, 1)
    assert deps.resolve_business_date(None) == clock.today()


def test_resolve_business_date报错写出真实参数名():
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date("abc", field="from_date")
    assert exc.value.detail == "from_date 参数须为 YYYY-MM-DD 格式"


# ---------------------------------------------------------------- 二、借它校验的五个调用方


@pytest.mark.parametrize(
    "path, param",
    [
        ("/api/appointments/doctors", "from_date"),
        ("/api/resources/match/slots", "from_date"),
        ("/api/analytics/patient-flow", "start"),
        ("/api/analytics/patient-flow", "end"),
        ("/api/audit/export", "until"),
    ],
)
def test_五个非today调用方_报错写对参数名且拒ISO变体(client, admin, path, param):
    for bad in ["abc", *ISO_VARIANTS]:
        resp = client.get(path, params={param: bad}, headers=admin)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json() == {"detail": f"{param} 参数须为 YYYY-MM-DD 格式"}, bad


def test_县域就诊率的分子分母用同一个日期窗口(client, admin):
    """修复前：`?start=20260901` 时县内按解析后的日期筛、县外按原串筛，
    县外就诊整段落空（实测 1 → 0）。现在这种写法在入口就被拒，
    合法写法下县外就诊照常计入。"""
    patient = client.post(
        "/api/patients",
        json={"name": "流向口径", "id_card": "330782198701017777"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": patient["id"], "visit_date": "2026-09-10",
              "external_org_name": "市一院"},
        headers=admin,
    )
    assert created.status_code == 201, created.text

    base = client.get(
        "/api/analytics/patient-flow", params={"start": "2026-09-01"}, headers=admin
    ).json()["outside_visits"]
    assert base >= 1
    assert client.get(
        "/api/analytics/patient-flow", params={"start": "20260901"}, headers=admin
    ).status_code == 422


# ---------------------------------------------------------------- 三、已接上 require_date 的筛选参数
#
# 逐模块接一批、加一批行。只读筛选的非法值此前多是 200 空集/错集（用户看到"没有数据"
# 而不是"日期写错了"），拼成时间戳去比 DateTime 列的在真 PG 上是 500。
# **留空一律等于不筛**——三端前端只在非空时发这些参数，但对接方可能发空串。
# 拼成时间戳去比 DateTime 列的那一族另列在 `test_date_filter_pg_dialect.py`：
# 它们的毛病只在真 PG 上现形（500），那份文件能被整份换到 PG 上再跑一遍。

#: (路径, 参数)。都用 admin 调：这里验的是入参校验，不是角色门。
FILTER_PARAMS = [
    ("/api/billing/reconciliation", "date"),
    ("/api/spd/revisits", "date_from"),
    ("/api/spd/revisits", "date_to"),
    ("/api/spd/followup-stats", "date_from"),
    ("/api/spd/followup-stats", "date_to"),
    ("/api/spd/followup-records", "date_from"),
    ("/api/spd/followup-records", "date_to"),
    ("/api/certs/death-report-cards/export.csv", "date_from"),
    ("/api/certs/death-report-cards/export.csv", "date_to"),
    ("/api/medwaste/handler-stats", "start_date"),
    ("/api/medwaste/handler-stats", "end_date"),
    ("/api/appointments/slots", "slot_date"),
    ("/api/mgmt/rosters", "duty_date"),
]

#: 形状错、日历上不存在、不补零、ISO 基本格式——前两类此前多是 200 空集，
#: 后两类在字符串比较下是**错集**（`"2026-9-1"` 比 2026 年所有补零日期都大）。
BAD_FILTER_VALUES = ["abc", "2026-02-31", "2026-9-1", "20260901"]


@pytest.mark.parametrize("path, param", FILTER_PARAMS)
def test_筛选日期非法值一律422且点名参数(client, admin, path, param):
    for bad in BAD_FILTER_VALUES:
        resp = client.get(path, params={param: bad}, headers=admin)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["detail"].startswith(f"{param}："), (bad, resp.json())


@pytest.mark.parametrize("path, param", FILTER_PARAMS)
def test_筛选日期留空等于不筛_合法值照常(client, admin, path, param):
    base = client.get(path, headers=admin)
    blank = client.get(path, params={param: ""}, headers=admin)
    assert base.status_code == 200, base.text
    assert blank.status_code == 200, blank.text
    assert blank.content == base.content, "留空应与不带这个参数完全一致"
    assert client.get(path, params={param: "2026-09-01"}, headers=admin).status_code == 200


# ---------------------------------------------------------------- 四、棘轮：日期查询参数只许走真源
#
# 自 `test_secondment_end_date_guard.py` 迁来（ADR-0024 第一步立的那条），并把判据
# 补全两处——迁移当天两处都量过：
#
# 1. **承认 `resolve_business_date` 是守卫**。它落到同一个 `check_date`（本文件第一节），
#    原判据只认 `require_date`，于是 `appointments.find_doctors` / `resources.match_slots`
#    的 `from_date` 被算成"裸 str"——它们其实一直有校验（误报 2 条）。
# 2. **不带 `date` 字样的日期参数也数进来**。原判据只看名字里有没有 `date`，
#    `access_logs.list_access_logs` 的 `start`/`end`、`spd.care.list_measurements`
#    的 `since`、`spd.followup.health_calendar` 的 `day` 都在分母之外（漏数 4 条）。
#    其中 `start`/`end` 与 `since` 拼成 `f"{x} 00:00:00"` 去比 DateTime 列——
#    与 spd 那三个端点同形状，真 PG 上非法值是 500。
#
# 净变化 25 → 27：不是新增欠账，是把原来没数到的数进来、把数错的剔出去。

#: 判据认的日期参数：名字含 `date`，或是下面这些约定俗成的日期参数名。
#: **按名字推导有盲区**——叫 `at` / `when` 的日期参数看不见；这里宁可写明，不假装全覆盖。
DATE_PARAM_NAMES = frozenset({"today", "start", "end", "since", "until", "day"})

#: 两个都落到 `datetypes.check_date`，参数经过其一即算守住。
GUARDS = frozenset({"require_date", "resolve_business_date"})

#: 还留在裸 `str`、未经上面守卫的日期查询参数（P1-58）。**只许变少。**
KNOWN_BARE_DATE_PARAMS: set[str] = {
    "routers/clinical_docs.py::list_handovers::handover_date",
    "routers/surgery.py::list_schedules::scheduled_date",
}

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"
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
    """函数体里经 `require_date(<名字>, ...)` / `resolve_business_date(<名字>, ...)` 的参数名。"""
    names = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in GUARDS:
            names.update(a.id for a in sub.args if isinstance(a, ast.Name))
    return names


def _plain_annotation(annotation) -> str:
    """`Annotated[str | None, Query()]` 取出 `str | None`——FastAPI 的常见写法，别让它绕过判据。"""
    if (
        isinstance(annotation, ast.Subscript)
        and ast.unparse(annotation.value) in ("Annotated", "typing.Annotated")
        and isinstance(annotation.slice, ast.Tuple)
    ):
        annotation = annotation.slice.elts[0]
    return ast.unparse(annotation)


def _date_params(func):
    for arg in list(func.args.args) + list(func.args.kwonlyargs):
        if arg.annotation is None:
            continue
        if _plain_annotation(arg.annotation) not in ("str", "str | None"):
            continue
        if "date" in arg.arg or arg.arg in DATE_PARAM_NAMES:
            yield arg.arg


def _scan() -> tuple[set[str], set[str]]:
    """(全部日期查询参数, 其中未经守卫的)。"""
    every, bare = set(), set()
    for path, func in _route_functions():
        guarded = _guarded_names(func)
        rel = path.relative_to(APP_DIR).as_posix()
        for name in _date_params(func):
            key = f"{rel}::{func.name}::{name}"
            every.add(key)
            if name not in guarded:
                bare.add(key)
    return every, bare


def _unguarded_date_params() -> set[str]:
    return _scan()[1]


def test_覆盖面自证():
    funcs = list(_route_functions())
    every, bare = _scan()
    print(
        f"\n[日期查询参数棘轮] 扫描 {len(funcs)} 个路由函数；日期查询参数 {len(every)} 处，"
        f"其中未经 require_date / resolve_business_date 的 {len(bare)} 处"
    )
    assert len(funcs) >= 500, f"只数到 {len(funcs)} 个路由函数，扫描面可能不对"
    # 补全的两处各自真的生效：不带 date 字样的参数数到了、经 resolve_business_date 的算守住了
    for entry in (
        "routers/users.py::export_audit_logs::until",
        "routers/analytics.py::patient_flow::start",
        "routers/appointments.py::find_doctors::from_date",
    ):
        assert entry in every, f"判据没数到 {entry}"
        assert entry not in bare, f"{entry} 经 resolve_business_date 校验，不该算裸 str"
    assert sum(1 for e in every if e.endswith("::today")) >= 30, "today 参数应被数进分母"


def test_判据看得穿Annotated写法():
    tree = ast.parse(
        "def f(a: Annotated[str | None, Query()] = None, b: Annotated[int, Query()] = 0,"
        " c_date: Annotated[str, Query()] = ''): pass"
    )
    assert list(_date_params(tree.body[0])) == ["c_date"]


def test_不得新增裸日期查询参数():
    new = sorted(_unguarded_date_params() - KNOWN_BARE_DATE_PARAMS)
    assert new == [], (
        "以下日期查询参数没走 deps.require_date——裸 `str` 只是个字符串，"
        "`2026-02-31` / `完全不是日期` 会原样入库或进入筛选条件"
        "（拼成时间戳去比 DateTime 列时，真 PG 上就是 500）：\n  "
        + "\n  ".join(new)
        + "\n\nbody 字段用 datetypes.DateStr / OptionalDateStr，查询参数用 deps.require_date"
        "（留空表示不筛选的，写成 `if x: x = require_date(x, field=\"x\")`）。"
    )


def test_名单只许变少():
    """接通一个就从名单里划掉；不划掉也红（否则名单会永远停在今天的数字）。"""
    stale = sorted(KNOWN_BARE_DATE_PARAMS - _unguarded_date_params())
    assert stale == [], (
        "这些已经走上 require_date（或已不存在）了，请从 KNOWN_BARE_DATE_PARAMS 划掉：\n  "
        + "\n  ".join(stale)
    )


def test_两条派驻结束端点已经不在名单里():
    """ADR-0024 第一步修的就是这两条——它们**必须**已经脱离欠账，否则那批用例在空转。"""
    unguarded = _unguarded_date_params()
    for entry in (
        "routers/admin_mgmt.py::end_secondment::end_date",
        "routers/staffing.py::end_secondment::end_date",
    ):
        assert entry not in unguarded, entry
        assert entry not in KNOWN_BARE_DATE_PARAMS, entry
