"""写侧越权判据的第四个盲区：**请求自己声明「这条新记录归哪家机构」**的写端点（P0-35）。

已有三条写侧棘轮各盯一种取法：路径带 id（`test_stage15_horizontal.py` 的按 id 写）、
body 带 id 去取**带 `org_id` 的对象**（`test_body_id_org_write_guard.py`）、挂在患者上的表（P1-71）。
它们问的都是「你能不能动**这条已有的**记录」。**新建时由请求声明归属**——`body.org_id` /
`from_org_id` / `managed_by_org_id`，或查询参数 `dispatched_to_org_id`——没有一条在看：
端点只 `db.get(Organization, body.org_id)` 查这家机构存不存在，而 `Organization` 自己没有
`org_id` 列，上面哪条棘轮的分母都数不到它。

2026-09-24 逐条判「按请求体里的患者号新建挂在患者上的行」时撞出：会诊申请的申请方、受邀方
两个机构列全由请求给，谁都能填。按形状量全仓：94 个写端点在请求里收机构号，**27 个没有任何守卫**。

判据：FastAPI 路由表里的写端点（居民端两个文件除外），请求体模型字段或查询参数里有名字以
`org_id` 结尾的，而端点（连同它传递调用到的本模块 helper，剥 docstring）没有任何守卫名、
也没有登记的领域守卫——两者与上游判据同一套，下面有用例比对两边一致。

三张名单：

- **自动豁免**：角色门是 `require_admin` 的。admin 属全域角色，`assert_org_writable` 对它恒放行，
  补上也是一句永不触发的守卫；从装饰器与参数里算出来，不手抄（前提由用例钉住）。
  `require_roles("director")` **不在此列**：自定义角色可整包复制 director 的权限点却不是全域角色（P0-28）；
- `BY_DESIGN`：请求里的机构**本来就是别家**（急救调度的目的医院、配置里的牵头机构），逐条写理由；
- `AWAITING_DECISION`：同一形状、但**早已登记为待业务裁定、故意未修**的（写明出处），只减不增；
- `KNOWN_UNGUARDED`：候选，只减不增；补上守卫就划掉（没划掉也红）。
"""
from __future__ import annotations

import ast
import functools
import pathlib

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]

#: 与 `test_stage15_horizontal._PATIENT_WRITE_GUARDS` 逐字相同（有用例比对）。
GUARDS = {
    "assert_obj_org_writable", "assert_org_writable", "assert_org_visible",
    "assert_patient_visible", "scope_org_list", "scope_patient_list", "log_patient_access",
}

#: 请求里的机构**本来就是别家**：给它补 `assert_org_writable` 等于把功能关掉。逐条写理由，只减不增。
BY_DESIGN = {
    "emergency.py:dispatch":
        "`dest_org_id` 是急救车送往的目的医院——120 调度按病情与路程派往任一家医院，目的医院"
        "本来就是别家；调度员所属机构不入库。（调度之后各环节由哪家记，是 P1-71 待裁定的急救一节。）",
    "spd/referral.py:create_referral_rule":
        "`target_org_id` 是转诊规则命中后建议转往的机构，规则是全县共用的配置，目标本来就是别家。"
        "（配置口径 `(\"director\", \"doctor\")` 放行了医师改全县配置，那是纵向权限问题，不在本闸门。）",
    "spd/config/catalog.py:update_program":
        "`lead_org_id` 是病种的牵头机构——全县病种目录的一项指定，不是「以某机构名义写入」的业务记录。"
        "（同上：配置角色含医师属纵向问题。）",
    "spd/config/centers.py:create_center":
        "`lead_org_id` 是专病中心的牵头机构，同上，是全县配置里的一项指定。",
}

#: 同一形状、但早已登记为「已实测越权但故意未修，等业务裁定」的——在这里补守卫等于替业务方做了决定。
AWAITING_DECISION = {
    "contracts.py:sign":
        "P1-45（docs/待裁定事项清单.md）：家医签约的签约机构由请求声明；县医院经办代乡镇院登记签约可能是"
        "正在用的合法流程，一刀切加 assert_org_writable 会掐掉它，要先答「跨机构代登记是否合法、走哪种授权」。",
}

#: 候选（2026-09-24 量）：请求声明机构、却没有任何归属判定。只减不增——补上守卫就从这里划掉。
KNOWN_UNGUARDED = {
    "chronic.py:register_chronic",
    "consultations.py:apply",
    "cssd.py:advance",
    "cssd.py:create_batch",
    "encounters.py:create_encounter",
    "exams.py:create_request",
    "infectious.py:report_case",
    "prescriptions.py:create_prescription",
    "referrals.py:create_referral",
    "spd/followup.py:auto_match_plans",
    "spd/followup.py:generate_report",
    "tcm.py:create_order",
}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PORTAL = {"portal.py", "spd/portal.py"}  # 居民端走门户令牌 + accessible_patient，与上游同一理由豁免


def _route_file(module: str) -> str:
    if module.startswith("app.spd.routers."):
        return "spd/" + module[len("app.spd.routers."):].replace(".", "/") + ".py"
    return module[len("app.routers."):].replace(".", "/") + ".py"


def _query_params(dependant) -> list[str]:
    names = [p.name for p in dependant.query_params]
    for sub in dependant.dependencies:
        names += _query_params(sub)
    return names


@functools.lru_cache(maxsize=1)
def _org_declaring_writes() -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """`文件:函数` → (文件名, 函数名, 请求里以 org_id 结尾的参数)。只看写方法的路由。"""
    from fastapi.routing import APIRoute

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
        if not (r.methods & _WRITE_METHODS):
            continue
        file_name = _route_file(r.endpoint.__module__)
        if file_name in _PORTAL:
            continue
        declared = [n for n in _query_params(r.dependant) if n.endswith("org_id")]
        for p in r.dependant.body_params:
            model = getattr(p.field_info, "annotation", None) or getattr(p, "type_", None)
            fields = getattr(model, "model_fields", None)
            if fields:
                declared += [f for f in fields if f.endswith("org_id")]
            elif p.name.endswith("org_id"):
                declared.append(p.name)
        if declared:
            key = f"{file_name}:{r.endpoint.__name__}"
            out[key] = (file_name, r.endpoint.__name__, tuple(sorted(set(declared))))
    return out


def _endpoint_node(tree: ast.AST, name: str):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name and n.decorator_list:
            return n
    raise AssertionError(f"源码里找不到端点函数 {name}")


def _admin_only(fn) -> bool:
    """角色门是 `require_admin`：装饰器的 dependencies 或参数默认值里挂着它。"""
    return "require_admin" in " ".join(ast.unparse(d) for d in fn.decorator_list) + ast.unparse(fn.args)


def _classify(sources: dict[str, str] | None = None) -> tuple[set[str], set[str]]:
    """返回 (无守卫的候选, 自动豁免的 admin-only)。`sources` 可按文件名换入改过的源码（自证用）。"""
    import test_stage15_horizontal as H

    files = dict(H._router_files())
    sources = sources or {}
    trees: dict[str, ast.AST] = {}
    unguarded: set[str] = set()
    admin_only: set[str] = set()
    for key, (file_name, fn_name, _declared) in _org_declaring_writes().items():
        if file_name not in trees:
            text = sources.get(file_name) or pathlib.Path(files[file_name]).read_text(encoding="utf-8")
            trees[file_name] = ast.parse(text)
        tree = trees[file_name]
        fn = _endpoint_node(tree, fn_name)
        body = H._with_local_helpers(tree, fn)
        if any(g in body for g in GUARDS) or H._has_domain_guard(file_name, tree, fn):
            continue
        (admin_only if _admin_only(fn) else unguarded).add(key)
    return unguarded, admin_only


def test_覆盖面自证():
    """防空转：路由表里收机构号的写端点不能是空的，判据也得真能认出守卫。"""
    declaring = _org_declaring_writes()
    unguarded, admin_only = _classify()
    print(
        f"\n[请求声明机构的写端点] {len(declaring)} 个；无守卫 {len(unguarded) + len(admin_only)}"
        f"（admin-only 自动豁免 {len(admin_only)}、按设计 {len(BY_DESIGN)}、待裁定 {len(AWAITING_DECISION)}、"
        f"候选 {len(KNOWN_UNGUARDED)}）"
    )
    assert len(declaring) >= 80, f"只认出 {len(declaring)} 个收机构号的写端点，路由表遍历多半坏了"
    assert "vaccination.py:vaccinate" in declaring and "vaccination.py:vaccinate" not in unguarded, \
        "接种登记（body.org_id + assert_org_writable）应当被认出、且算作已守卫"


def test_守卫名单与上游判据保持一致():
    import test_stage15_horizontal as H

    assert GUARDS == H._PATIENT_WRITE_GUARDS, "两边的守卫名单分叉了：上游加了新守卫，这边会把它当没守卫"


def test_admin_only豁免的前提():
    """自动豁免只在「require_admin 只放 admin、admin 属全域角色」时成立——两条任一变了，豁免要重判。"""
    import inspect

    from app import deps, visibility

    assert 'user.role != "admin"' in inspect.getsource(deps.require_admin)
    assert "admin" in visibility.GLOBAL_ROLES


def test_不得新增请求声明机构的无守卫写端点():
    unguarded, _ = _classify()
    registers = (KNOWN_UNGUARDED, set(BY_DESIGN), set(AWAITING_DECISION))
    assert sum(len(r) for r in registers) == len(set().union(*registers)), "同一条只能登记在一张名单里"
    new = sorted(unguarded - KNOWN_UNGUARDED - set(BY_DESIGN) - set(AWAITING_DECISION))
    assert new == [], (
        "以下写端点由请求声明新记录归哪家机构，却没有任何归属判定——`db.get(Organization, …)` "
        "只查存在不查归属：\n  " + "\n  ".join(new)
        + "\n\n补 assert_org_writable(db, user, body.xxx_org_id)；若请求里的机构按设计就是别家，"
        "写明理由登记进 BY_DESIGN。"
    )


def test_名单只许变少():
    unguarded, _ = _classify()
    stale = sorted((KNOWN_UNGUARDED | set(BY_DESIGN) | set(AWAITING_DECISION)) - unguarded)
    assert stale == [], "这些已补上守卫（或已不存在），请从名单里划掉：\n  " + "\n  ".join(stale)


def test_判据自证_拿掉接种登记的守卫当场点名():
    """把 `vaccinate` 的 `assert_org_writable(db, user, body.org_id)` 删掉，判据必须点名它。"""
    import test_stage15_horizontal as H

    path = dict(H._router_files())["vaccination.py"]
    text = pathlib.Path(path).read_text(encoding="utf-8")
    guard = "    assert_org_writable(db, user, body.org_id)\n"
    start = text.index("def vaccinate(")
    end = text.index("\n@router", start)
    assert guard in text[start:end], "vaccinate 里找不到机构守卫行，自证前提变了"
    reverted = text[:start] + text[start:end].replace(guard, "", 1) + text[end:]
    assert "vaccination.py:vaccinate" in _classify({"vaccination.py": reverted})[0]
    assert "vaccination.py:vaccinate" not in _classify()[0]
