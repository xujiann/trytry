"""读侧：GET 在查询参数里收机构号，却只拿 `resolve_org_scope` 当范围（P0-37）。

`deps.resolve_org_scope` 是**筛选器不是授权器**（`visibility` 模块 docstring 第一段）：`?org_id=X`
原样变成 `[X]`，不给就是全域。第九轮把「带上 `?org_id=甲` 就拉到甲院职工名册」那一批换成了
`scope_org_list`，可统计 / 报表类接口一直只用它：2026-09-24 实测，一家与谁都没有关系的新卫生院，
医生 / 经办读到县医院的合并报表、运行效率、药占比、床位统计（已修，见 `test_stats_org_scope_guard.py`）。

判据：FastAPI 路由表里的 GET（居民端两个文件除外），查询参数里有名字以 `org_id` 结尾的，**或者**端点
（连同它传递调用的本模块 helper，剥 docstring）拿 `resolve_org_scope` 取范围的，而里面没有任何授权判定——
**`resolve_org_scope` 不算**。`require_admin` 的角色门自动豁免（admin 属全域角色）。量出 72 个收机构号的 GET，
修掉四个统计之后余下 16 个无授权判定；后一半判据是同日补的：只收片区号（`group_id`）、不收机构号的统计接口
一样把 `resolve_org_scope` 当范围，只认参数名时 6 个掉在分母外（P0-37 第二层）。逐条判过，分两张名单，只减不增：

- `BY_DESIGN`：跨机构可见就是用途（便捷寻医、号源 / 手术间撮合、资源目录、县域监测……），逐条写理由；
- `AWAITING`：该按什么范围给看要人定（写明出处），答之前不改。
"""
from __future__ import annotations

import ast
import functools
import pathlib

#: 读侧算作授权判定的名字。`resolve_org_scope` **刻意不在里面**。
READ_GUARDS = {
    "scope_org_list", "scope_stats_orgs", "assert_org_visible", "visible_org_ids", "stats_org_ids",
    "assert_org_writable", "assert_obj_org_writable", "assert_patient_visible", "scope_patient_list",
    "log_patient_access",
}

BY_DESIGN = {
    "access_logs.py:list_access_logs":
        "合规排查：路由只放 director（全县稽核）；针对单个患者的查询本身也留痕。",
    "appointments.py:find_doctors":
        "便捷寻医：docstring 写明第九轮「明确不设限」——跨机构找医师、约号正是用途，医师与号源本就公开挂出。",
    "resources.py:match_operating_rooms":
        "手术间撮合：docstring 写明「明确不设限」——基层转上来要先看得到县医院哪天有空台，空档不是敏感数据。",
    "resources.py:match_slots":
        "号源撮合：回答「能排到哪里、最早什么时候」，跨机构比较余量正是用途，与手术间撮合同一口径。",
    "resources.py:resource_catalog":
        "统一资源目录：号源 / 手术间 / 血制品 / 通用资源「有什么、在哪、还能用多少」，是医共体共享的聚合视图。",
    "surveillance.py:list_syndromes":
        "症候群监测日报：按机构的病例数（无个体），县域多点触发监测的底数——预警对一线开放（test_县域监测预警对一线保持开放）。",
    "surveillance.py:list_resources":
        "应急物资与队伍储备：突发事件调度要看得到全县储备，不是经营数据。",
    "performance.py:org_scorecards":
        "绩效考核打分：路由整体只放 director（管理层考核口径）。",
    "spd/assess.py:list_point_accounts":
        "员工积分榜（签到、任务得分），不是患者数据，也不是经营数据。",
    "spd/config/teams.py:list_teams":
        "慢专病服务团队目录（团队、所属机构、病种），派单与转诊要跨机构选团队。",
    "spd/config/teams.py:list_village_doctors":
        "村医目录（姓名、乡镇、村），与服务团队目录同理。",
    "spd/config/devices.py:list_devices":
        "设备台账（设备号、型号、绑定的患者号，无身份字段），已登记在无身份读接口的「仅聚合」一层"
        "（test_unscopable_patient_reads.AGGREGATE_ONLY_READS）。",
    # ---- 第二层（只收 group_id）----
    "surveillance.py:multi_point_alerts":
        "多点触发预警：给一线的暴发信号，`test_stage15_horizontal.py::test_县域监测预警对一线保持开放` 钉着不许收紧。",
    "surveillance.py:readiness":
        "应急资源保障（缺口与过期）：与应急物资储备清单（本名单的 `surveillance.py:list_resources`）同一口径，突发事件调度要看全县。",
    "vaccine_supply.py:vaccination_stats":
        "接种剂次与 AEFI 发生率、冷链异常：县域公卫监测指标，只有聚合数、无个体，与症候群监测日报同一口径。",
}

AWAITING = {
    "quality.py:clinical_indicators":
        "医疗质量指标（诊断符合率、死亡率、抢救成功率）：已在无身份患者读接口第一层名单里待裁定"
        "（test_unscopable_patient_reads.UNSCOPABLE_PATIENT_READS / P1-49）。",
    "projects.py:list_projects":
        "项目台账（预算、进度、里程碑）：按本机构、按片区（接口带 group_id）还是全县看，见待裁定清单 P0-37 一节。",
    "resources.py:list_resources":
        "通用资源清单：未发布的资源别家看不看得到，见待裁定清单 P0-37 一节。",
    "staffing.py:list_secondments":
        "派驻台账（人员姓名、职称、派出 / 接收机构）：人员下沉监测按片区还是全县看；与 mgmt 那套派驻两套实现"
        "同在待裁定（P1-56），见待裁定清单 P0-37 一节。",
    # ---- 第二层（只收 group_id）----
    "disease_programs.py:program_stats":
        "专病项目统计：已在无身份患者读接口第一层名单里待裁定（test_unscopable_patient_reads.UNSCOPABLE_PATIENT_READS）。",
    "projects.py:project_stats":
        "项目总览（按状态、逾期、平均进度的聚合数）：与项目台账同一个问题——按本机构、片区还是全县看，见待裁定清单 P0-37 一节。",
    "staffing.py:dispatch_stats":
        "下沉调度统计（按接收机构的在派与满 6 个月人数）：正是派驻台账那一问里的「县级监测指标」，同在待裁定清单 P0-37 一节。",
}

_PORTAL = {"portal.py", "spd/portal.py"}


def _route_file(module: str) -> str:
    if module.startswith("app.spd.routers."):
        return "spd/" + module[len("app.spd.routers."):].replace(".", "/") + ".py"
    return module[len("app.routers."):].replace(".", "/") + ".py"


def _query_params(dependant) -> list[str]:
    names = [p.name for p in dependant.query_params]
    for sub in dependant.dependencies:
        names += _query_params(sub)
    return names


def _admin_only(dependant) -> bool:
    """路由（含 router 级 dependencies）挂了 require_admin。"""
    for sub in dependant.dependencies:
        if getattr(sub.call, "__name__", "") == "require_admin" or _admin_only(sub):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _org_param_reads() -> dict[str, tuple[str, str, bool]]:
    """`文件:函数` → (文件名, 函数名, 是否 admin-only)。

    收两种 GET：查询参数里有 *org_id 的；函数体（连同本模块 helper）拿 `resolve_org_scope` 取范围的——
    后者只收 `group_id` 也算（第二层，见模块 docstring）。
    """
    from fastapi.routing import APIRoute

    import test_stage15_horizontal as H
    from app.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            orig = getattr(r, "original_router", None)
            if orig is not None:
                yield from walk(orig.routes)

    files = dict(H._router_files())
    trees: dict[str, ast.AST] = {}
    out = {}
    for r in walk(app.routes):
        if "GET" not in r.methods:
            continue
        file_name = _route_file(r.endpoint.__module__)
        if file_name in _PORTAL:
            continue
        key = f"{file_name}:{r.endpoint.__name__}"
        if not any(n.endswith("org_id") for n in _query_params(r.dependant)):
            if file_name not in files:  # 路由不在 app/routers 与 app/spd/routers 里（如 main.py 的健康检查）
                continue
            if file_name not in trees:
                trees[file_name] = ast.parse(pathlib.Path(files[file_name]).read_text(encoding="utf-8"))
            fn = next(n for n in ast.walk(trees[file_name])
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == r.endpoint.__name__ and n.decorator_list)
            if "resolve_org_scope" not in H._with_local_helpers(trees[file_name], fn):
                continue
        out[key] = (file_name, r.endpoint.__name__, _admin_only(r.dependant))
    return out


def _unguarded(sources: dict[str, str] | None = None) -> set[str]:
    import test_stage15_horizontal as H

    files = dict(H._router_files())
    sources = sources or {}
    trees: dict[str, ast.AST] = {}
    out: set[str] = set()
    for key, (file_name, fn_name, admin_only) in _org_param_reads().items():
        if admin_only:
            continue
        if file_name not in trees:
            text = sources.get(file_name) or pathlib.Path(files[file_name]).read_text(encoding="utf-8")
            trees[file_name] = ast.parse(text)
        tree = trees[file_name]
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name and n.decorator_list)
        body = H._with_local_helpers(tree, fn)
        if any(g in body for g in READ_GUARDS) or H._has_domain_guard(file_name, tree, fn):
            continue
        out.add(key)
    return out


def test_覆盖面自证():
    reads = _org_param_reads()
    unguarded = _unguarded()
    print(f"\n[收机构号或拿 resolve_org_scope 取范围的 GET] {len(reads)} 个；无授权判定 {len(unguarded)}（按设计 {len(BY_DESIGN)}、待裁定 {len(AWAITING)}）")
    assert len(reads) >= 60, f"只认出 {len(reads)} 个收机构号的 GET，路由表遍历多半坏了"
    assert "mgmt.py:list_employees" in reads or any(k.endswith(":list_employees") for k in reads), \
        "职工名册（第九轮修过的那批）应当在分母里"


def test_resolve_org_scope不算授权():
    """前提：它今天仍只解析范围、不看调用方——哪天它自己收了 user，这条闸门要重想。"""
    import inspect

    from app import deps

    assert "user" not in inspect.signature(deps.resolve_org_scope).parameters
    assert "resolve_org_scope" not in READ_GUARDS


def test_不得新增只拿resolve_org_scope当范围的读接口():
    assert not (set(BY_DESIGN) & set(AWAITING)), "同一条只能登记在一张名单里"
    new = sorted(_unguarded() - set(BY_DESIGN) - set(AWAITING))
    assert new == [], (
        "以下 GET 在查询参数里收机构号，却没有任何授权判定（resolve_org_scope 只解析范围、不看调用方）：\n  "
        + "\n  ".join(new)
        + "\n\n统计类收进 scope_stats_orgs（本机构 + 同医共体），明细类走 scope_org_list；"
        "跨机构可见就是用途的，写明理由登记进 BY_DESIGN。"
    )


def test_名单只许变少():
    stale = sorted((set(BY_DESIGN) | set(AWAITING)) - _unguarded())
    assert stale == [], "这些已补上判定（或已不存在），请从名单里划掉：\n  " + "\n  ".join(stale)


def test_判据自证_拿掉合并报表的收口当场点名():
    import test_stage15_horizontal as H

    path = dict(H._router_files())["accounting.py"]
    text = pathlib.Path(path).read_text(encoding="utf-8")
    fixed = "    scope = scope_stats_orgs(db, user, resolve_org_scope(db, group_id, org_id))\n"
    start = text.index("def consolidated_statements(")
    end = text.find("\n@router", start)
    end = len(text) if end == -1 else end  # 它是文件里最后一个端点
    assert fixed in text[start:end], "合并报表里找不到 P0-37 的收口行，自证前提变了"
    reverted = text[:start] + text[start:end].replace(fixed, "    scope = resolve_org_scope(db, group_id, org_id)\n", 1) + text[end:]
    assert "accounting.py:consolidated_statements" in _unguarded({"accounting.py": reverted})
    assert "accounting.py:consolidated_statements" not in _unguarded()
