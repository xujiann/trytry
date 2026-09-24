"""读侧：按 id 读「有机构列、不挂患者」的表，不判调用方能不能看这家机构（P0-38）。

写侧早就有按 id 的机构棘轮（`test_stage15_horizontal.py` 的 `_byid_org_write_endpoints`），
患者那一族的按 id 读也有（同文件 `_patient_byid_read_endpoints`），唯独**按 id 读机构归属数据**
没人盯：2026-09-24 实测，一家与谁都没有关系的新卫生院，经办 / 医生按 id 读到县医院的会计凭证、
职工人事变动史、物资出入库流水，别家实训的报名名单与考核成绩，别家审批流每一步的意见——
而这些表的清单早就只给看本机构。清单拦住的，顺着 id 读明细原样放出来（已修，见
`test_management_detail_org_read.py`、`test_training_roster_org_read.py`、`test_workflow_instance_org_read.py`）。

判据：FastAPI 路由表里带路径参数的 GET（居民端两个文件除外），端点（连同它传递调用的本模块 helper，
剥 docstring）`db.get` 了一张**有机构列（列名以 `org_id` 结尾）、没有 `patient_id` 列**的表，
却没有任何读侧授权判定（名单与 `test_org_param_read_guard.READ_GUARDS` 共用一份）。
`require_admin` 的角色门自动豁免。量出 18 条，修掉 5 条（另有 3 条同形状的清单 / 待办顺手修了），
余下 13 条逐条判过，分三张名单，只减不增：

- `BY_DESIGN`：跨机构可见就是用途（组织拓扑、病种与路径配置、服务团队目录……），逐条写理由；
- `ELSEWHERE`：真正要守的是患者数据，已登记在别的欠账名单里、有人在盯——这里只点名出处，
  并有一条用例核对出处名单里确实还有它（出处划掉了，这里也得划）；
- `AWAITING`：该按什么范围给看要人定（写明出处），答之前不改。

⚠️ **判据看不见的**：不 `db.get` 父对象、直接按路径参数查子表的读接口（修第二批时撞上的
`education:list_assessments` 就是这样漏掉的——它连计划在不在都不查）。同形状在 2026-09-24
（`c5a7fcc` 之上）量过：带路径参数、无任何判定、也不 `db.get` 机构 / 患者表的 GET 有 27 条，
其中挂在患者上的已在 P1-69 名单里，其余登记为 P1-77，逐条判之前不扩这条判据。
"""
from __future__ import annotations

import ast
import functools
import pathlib
import re

BY_DESIGN = {
    "org_groups.py:list_members":
        "分组成员（机构名、层级、加入时间）是组织架构拓扑：同文件 `groups_of_org` 的 docstring 写明第九轮"
        "「明确不设限」——转诊、调拨、统计口径都要引用，且不含任何经营或诊疗数据；分组清单同样不设限。",
    "infectious.py:case_report_card":
        "法定传染病报告卡导出：路由只放 director（全域角色；自定义角色须被单独授予这个权限点），"
        "且病例登记不含患者个体标识（docstring 写明，`infectious_cases` 只记报告机构 / 病种 / 发病日期）。",
    "spd/config/catalog.py:get_program":
        "专病档案（病种目录、入组标准、管理目标）是全县共用的病种配置：清单同样不设限，"
        "`lead_org_id` 是牵头机构而不是归属——各家按同一套标准管病人。",
    "spd/config/paths.py:get_path_template":
        "临床路径模板（节点、时限、执行角色）是诊疗规范配置：清单同样不设限；"
        "复制 / 修改另按归属判（`copy_path_template` 等写接口有 `assert_org_writable`）。",
    "spd/config/teams.py:get_team":
        "服务团队详情（成员、分工、授权开关）：与团队目录同一口径——派单与转诊要跨机构选团队，"
        "目录已登记在 `test_org_param_read_guard.BY_DESIGN`（`spd/config/teams.py:list_teams`）。",
    "spd/config/teams.py:village_doctor_qr":
        "村医绑定二维码：与村医目录同一口径（`test_org_param_read_guard.BY_DESIGN` 的 `list_village_doctors`）。"
        "⚠️ 码里的 `bind_token` 今天**没有任何端点消费**（医生移动端只把 hash 当页签），所以是惰性的；"
        "哪天接上扫码绑定，它就成了凭据——这条与村医清单回执里的明文 `bind_token` 要一起重判（P1-78）。",
}

#: 真正要守的是患者数据，已在别的欠账名单里：`文件:函数` → `模块.名单`。
ELSEWHERE = {
    "disease_programs.py:get_enrollment": "test_stage15_horizontal.NEWLY_VISIBLE_UNGUARDED_READS",
    "disease_programs.py:program_stats": "test_unscopable_patient_reads.UNSCOPABLE_PATIENT_READS",
    "pharmacy.py:batch_dispense_trace": "test_list_pagination_ratchet.HELD_PENDING_SCOPE_DECISION",
    "spd/tasks.py:get_path_instance": "test_stage15_horizontal.NEWLY_VISIBLE_UNGUARDED_READS",
}

AWAITING = {
    "projects.py:get_project":
        "项目详情：写侧已按机构收口，读侧有意未改（`test_projects_resources_org_guard.py::"
        "test_读侧本轮未改是有意的` 钉着）——按本机构、按片区还是全县看，与项目台账同在待裁定清单 P0-37 一节。",
    "vaccine_supply.py:batch_recipients":
        "疫苗批次反查接种对象（受种者名单）：P1-49 第四批扩大时登记在册的无收口端点之一，"
        "与药品批次反查（`pharmacy:batch_dispense_trace`）同一族，等「这些表谁该看见全部」的裁定。",
    "spd/followup.py:get_report_instance":
        "慢专病报告实例（机构运营汇总 + 订阅人）：清单也不收口；按生成机构的统计范围（本机构 + 同医共体）给看，"
        "还是订阅人另开一道，全域报告（不挂机构）谁能看——见待裁定清单 P0-38 一节。",
}


@functools.lru_cache(maxsize=1)
def _org_owned_models() -> frozenset[str]:
    """有机构列（列名以 `org_id` 结尾）、没有 `patient_id` 列的模型类名。"""
    from app import models

    out = set()
    for mapper in models.Base.registry.mappers:
        cols = {c.name for c in mapper.class_.__table__.columns}
        if "patient_id" not in cols and any(c.endswith("org_id") for c in cols):
            out.add(mapper.class_.__name__)
    return frozenset(out)


@functools.lru_cache(maxsize=1)
def _byid_reads() -> dict[str, tuple[str, str, bool]]:
    """`文件:函数` → (文件名, 函数名, 是否 admin-only)。只收带路径参数的 GET。"""
    from fastapi.routing import APIRoute

    import test_org_param_read_guard as R
    from app.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            orig = getattr(r, "original_router", None)
            if orig is not None:
                yield from walk(orig.routes)

    out = {}
    for r in walk(app.routes):
        if "GET" not in r.methods or "{" not in r.path:
            continue
        file_name = R._route_file(r.endpoint.__module__)
        if file_name in R._PORTAL:
            continue
        out[f"{file_name}:{r.endpoint.__name__}"] = (file_name, r.endpoint.__name__, R._admin_only(r.dependant))
    return out


def _scan(sources: dict[str, str] | None = None) -> tuple[set[str], set[str]]:
    """(按 id 取机构归属表的 GET 全集, 其中无读侧判定的)。"""
    import test_org_param_read_guard as R
    import test_stage15_horizontal as H

    files = dict(H._router_files())
    sources = sources or {}
    org_models = _org_owned_models()
    trees: dict[str, ast.AST] = {}
    reads: set[str] = set()
    unguarded: set[str] = set()
    for key, (file_name, fn_name, admin_only) in _byid_reads().items():
        if admin_only:
            continue
        if file_name not in trees:
            text = sources.get(file_name) or pathlib.Path(files[file_name]).read_text(encoding="utf-8")
            trees[file_name] = ast.parse(text)
        tree = trees[file_name]
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name and n.decorator_list)
        body = H._with_local_helpers(tree, fn)
        if not set(re.findall(r"db\.get\((\w+),", body)) & org_models:
            continue
        reads.add(key)
        if any(g in body for g in R.READ_GUARDS) or H._has_domain_guard(file_name, tree, fn):
            continue
        unguarded.add(key)
    return reads, unguarded


def test_覆盖面自证():
    reads, unguarded = _scan()
    listed = len(BY_DESIGN) + len(ELSEWHERE) + len(AWAITING)
    print(f"\n[按 id 读机构归属表的 GET] {len(reads)} 个；无读侧判定 {len(unguarded)}"
          f"（按设计 {len(BY_DESIGN)}、别处在盯 {len(ELSEWHERE)}、待裁定 {len(AWAITING)}，共 {listed}）")
    assert len(reads) >= 25, f"只认出 {len(reads)} 个，路由表遍历或模型推导多半坏了"
    for fixed in ("accounting.py:get_voucher", "admin_mgmt.py:list_employee_changes",
                  "education.py:list_enrollments", "workflows.py:instance_history"):
        assert fixed in reads, f"P0-38 修过的 {fixed} 应当在分母里（它 db.get 了机构归属表）"
    assert "Voucher" in _org_owned_models() and "Patient" not in _org_owned_models()


def test_不得新增按id读机构归属表而不判可见的接口():
    names = [set(BY_DESIGN), set(ELSEWHERE), set(AWAITING)]
    assert not (names[0] & names[1] or names[0] & names[2] or names[1] & names[2]), "同一条只能登记在一张名单里"
    _, unguarded = _scan()
    new = sorted(unguarded - set(BY_DESIGN) - set(ELSEWHERE) - set(AWAITING))
    assert new == [], (
        "以下 GET 按 id 取了有机构列的表，却没有任何读侧判定：\n  " + "\n  ".join(new)
        + "\n\n明细类取出对象后按它的机构判 assert_org_visible（本机构；全域角色看全县），"
        "统计类收进 scope_stats_orgs；跨机构可见就是用途的，写明理由登记进 BY_DESIGN。"
    )


def test_名单只许变少():
    _, unguarded = _scan()
    stale = sorted((set(BY_DESIGN) | set(ELSEWHERE) | set(AWAITING)) - unguarded)
    assert stale == [], "这些已补上判定（或已不存在），请从名单里划掉：\n  " + "\n  ".join(stale)


def test_别处在盯的出处名单里确实还有它():
    import importlib

    missing = []
    for key, where in ELSEWHERE.items():
        module, attr = where.split(".")
        if key not in getattr(importlib.import_module(module), attr):
            missing.append(f"{key}（{where}）")
    assert missing == [], "出处名单里已经划掉了，这里也要重判：\n  " + "\n  ".join(missing)


def test_判据自证_拿掉凭证明细的判定当场点名():
    import test_stage15_horizontal as H

    path = dict(H._router_files())["accounting.py"]
    text = pathlib.Path(path).read_text(encoding="utf-8")
    fixed = "    assert_org_visible(db, user, voucher.org_id)  # P0-38：凭证清单只给看本机构，明细同一口径\n"
    start = text.index("def get_voucher(")
    end = text.index("\n@router", start)
    assert fixed in text[start:end], "凭证明细里找不到 P0-38 的判定行，自证前提变了"
    reverted = text[:start] + text[start:end].replace(fixed, "", 1) + text[end:]
    assert "accounting.py:get_voucher" in _scan({"accounting.py": reverted})[1]
    assert "accounting.py:get_voucher" not in _scan()[1]
