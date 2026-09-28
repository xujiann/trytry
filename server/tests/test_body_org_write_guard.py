"""入参承载「以谁的名义写」的写接口，必须校验机构归属——P1-39 的闸门。

## 为什么要有这一条

平台已有两道横向越权闸门，**判据都不看请求体**：

* `test_stage15_horizontal.py`：认的是「被 `db.get(M, ...)` 直取、且 `M` 自己带
  `org_id` 列」的读写；
* `test_cross_org_write_guards.py`：三条手写行为用例，认的是 `/{id}` 型直取、
  归属隔一跳外键的对象。

**机构标识从请求体进来的写接口，两道都看不见。** 这个盲区是补 `POST /api/medwaste`
时发现的：它只查机构**存在**、不查调用者能不能以这家机构的名义写，而同模块另外
三个写接口都查了。修完那一处后按同一形状全仓实打，**12 个探针里 9 个放行**——
乙卫生院的医师能以甲县医院的名义开处方、报传染病卡、开检查单、上转患者；
乙院的经办能从甲院药房**调出药品**、把**甲院的职工**派驻出去。

后果分三档：把业务记到别家账上（就诊/签约/报卡，监管报数失真）、
以别家名义对外发起（转诊/会诊/检查申请）、**动别家的实物与人**（调拨/派驻）。

## 判据

1. **机构外键列名从模型元数据推导**（凡指向 `organizations.id` 的列），不手写。
   新增一个机构外键列自动进入分母。
2. 列名分两类，这是本文件**唯一**需要人判断的地方，而且判的是**列名**（13 个、
   稳定），不是逐个端点：
   - **acting（以谁的名义写）**：`org_id` / `from_org_id` / `initiator_org_id` /
     `current_org_id` —— 必须校验；
   - **counterparty（写给谁）**：`to_org_id` / `target_org_id` / `dest_org_id` 等
     —— 跨机构正是业务本身（转诊的接收方、急救的目的医院），不能校验。
   **fail-closed**：新出现的机构外键列名若两边都没登记，一律当 acting 处理
   （宁可误报，漏报的代价是下一次静默越权），且 `test_列名分类没有漏网` 会直接变红。
3. **仅 `require_admin` 可达的端点自动豁免**，不需要手写豁免条目——`require_admin`
   直接比 `user.role != "admin"`，没有自定义角色旁路，而 admin 是全域角色，
   `assert_org_writable` 对它恒真。**`require_roles(...)` 不算**：它会放行凭权限点
   的自定义角色，而自定义角色不是全域角色（这正是 `test_stage11_security` 那条
   用例踩到的交互）。

## 认不出的形态（如实声明）

* 机构标识藏在嵌套模型里（本闸门只看请求模型的一层字段）；
* 写接口从别的表**推导**出机构而不是从入参取（那属于既有两道闸门的射程）；
* 校验写在被调用的 helper 里而不是 handler 源码中——本闸门按 handler 源文本判，
  这类会被误报成缺口，届时按实际情况补进 `EXEMPT` 并写明理由。
"""
from __future__ import annotations

import ast
import inspect
import warnings
from pathlib import Path

from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.database import Base
from app.main import app

WRITE = {"POST", "PUT", "PATCH", "DELETE"}
GUARDS = ("assert_org_writable", "assert_obj_org_writable")

#: 「以谁的名义写」——必须校验归属
ACTING = {"org_id", "from_org_id", "initiator_org_id", "current_org_id",
          # P1-58 新增的两列：急救的调度方、承接的中药房。二者都由服务端从操作人身上取、
          # 不收请求体；归进 acting 是 fail-closed——哪天有人把它们开放成入参，
          # 那就是调用方自报"我以哪家的名义在做"，必须校验
          "dispatch_org_id", "pharmacy_org_id",
          # P1-59：预约的代约机构，同样由服务端取自经办人
          "booked_org_id"}
#: 「写给谁」——跨机构是业务本身，不能校验
COUNTERPARTY = {
    "to_org_id": "转诊/会诊/调拨的接收方，跨机构正是这些业务的全部意义",
    "target_org_id": "慢专病转诊规则与转诊单的目标机构",
    "dest_org_id": "急救送达的目的医院",
    "grantee_org_id": "患者授权给哪家机构调阅，被授权方本就是别家",
    "dispatched_to_org_id": "消毒供应批次发往的机构",
    "center_org_id": "消毒供应中心（共享中心按设计服务多家）",
    "lead_org_id": "分组/病种/中心的牵头机构，由管理侧配置",
    "managed_by_org_id": "慢病归口管理机构，可以不是建档机构",
    "claimed_org_id": "共享诊断中心认领方，认领的本就是别家开的单",
    "parent_id": "organizations 自引用的上级机构，不是「以谁的名义写」",
}

#: 手写豁免：只减不增，每条须写明为什么该端点不需要归属校验。
EXEMPT: dict[str, str] = {}


def _org_fk_columns() -> set[str]:
    return {
        c.name
        for t in Base.metadata.tables.values()
        for c in t.columns
        for fk in c.foreign_keys
        if fk.column.table.name == "organizations"
    }


def _walk(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        elif hasattr(r, "routes") and not isinstance(r, APIRoute):
            yield from _walk(r.routes)


def _admin_only(route: APIRoute) -> bool:
    """整条依赖链里出现 `require_admin` 才算——见模块 docstring 第 3 条。"""
    def _has(deps) -> bool:
        for d in deps:
            if getattr(getattr(d, "call", None), "__name__", "") == "require_admin":
                return True
            if _has(getattr(d, "dependencies", [])):
                return True
        return False
    return _has(route.dependant.dependencies)


class _Scan:
    def __init__(self) -> None:
        org_fk = _org_fk_columns()
        self.org_fk = org_fk
        self.unclassified = sorted(org_fk - ACTING - set(COUNTERPARTY))
        # fail-closed：没登记的列名当 acting 处理
        acting = (ACTING | set(self.unclassified)) & org_fk
        self.write_routes = 0
        self.in_scope: list[tuple[str, str, str, list[str]]] = []
        self.guarded: list[str] = []
        self.admin_only: list[str] = []
        self.violations: list[str] = []
        seen: set[int] = set()
        for r in _walk(app.routes):
            if id(r) in seen or not (r.methods & WRITE):
                continue
            seen.add(id(r))
            self.write_routes += 1
            fields: set[str] = set()
            for name, p in inspect.signature(r.endpoint).parameters.items():
                ann = p.annotation
                if isinstance(ann, type) and issubclass(ann, BaseModel):
                    fields |= {f for f in ann.model_fields if f in acting}
                elif name in acting:
                    fields.add(name)
            if not fields:
                continue
            key = f"{r.endpoint.__module__}:{r.endpoint.__name__}"
            method = sorted(r.methods & WRITE)[0]
            self.in_scope.append((method, r.path, key, sorted(fields)))
            if key in EXEMPT:
                continue
            if any(g in inspect.getsource(r.endpoint) for g in GUARDS):
                self.guarded.append(key)
            elif _admin_only(r):
                self.admin_only.append(key)
            else:
                self.violations.append(f"{method} {r.path} → {key} {sorted(fields)}")


RESULT = _Scan()

#: 缺口欠账：只许变小。本轮从 15 清到 0。
BASELINE_VIOLATIONS = 0


def test_覆盖面自证() -> None:
    """闸门要自己说清扫了什么——不声张覆盖范围的绿灯和假装看过全部的哨兵一样危险。"""
    lines = [
        "",
        "[请求体机构归属守卫] 覆盖面自证",
        f"  机构外键列名：{len(RESULT.org_fk)} 个（从模型元数据推导，非手写）"
        f" = acting {len(ACTING & RESULT.org_fk)} + counterparty {len(set(COUNTERPARTY) & RESULT.org_fk)}"
        f" + 未分类 {len(RESULT.unclassified)}（fail-closed，按 acting 处理）",
        f"  扫描写端点：{RESULT.write_routes} 个（运行期路由树全量，穿过 include_router 封装）",
        f"  入参带 acting 字段的：{len(RESULT.in_scope)} 个"
        f" = 已校验 {len(RESULT.guarded)} + 仅admin可达自动豁免 {len(RESULT.admin_only)}"
        f" + 手写豁免 {len(EXEMPT)} + **缺口 {len(RESULT.violations)}**（基线 {BASELINE_VIOLATIONS}）",
        "  认不出的形态：嵌套模型里的机构字段、从别表推导机构的写接口、"
        "校验写在被调 helper 里的（见模块 docstring）",
    ]
    report = "\n".join(lines)
    print(report)
    warnings.warn(report, UserWarning, stacklevel=2)
    # 反空转：任一落脚点为 0 都说明判据失灵，而不是"真的很干净"
    # 486 是本次实测值（与 test_authz_matrix 报的「写接口 486 个」对得上）。
    # 钉一个略低的下限：掉下去说明路由树没走全，而不是"真的少了这么多端点"。
    assert RESULT.write_routes > 400, (
        f"写端点只扫到 {RESULT.write_routes} 个，路由树没走全"
    )
    assert len(RESULT.in_scope) > 50, "acting 字段一个都没认出来 = 判据空转"
    assert len(RESULT.guarded) > 20, "已校验的一个都没认出来 = 判据空转"


def test_列名分类没有漏网() -> None:
    """新增机构外键列必须明确归到 acting 或 counterparty——fail-closed 只保证
    不漏报，不代替人的判断：被当成 acting 却其实是 counterparty，就会误报。"""
    assert RESULT.unclassified == [], (
        f"以下机构外键列名没有分类：{RESULT.unclassified}。"
        " 它是「以谁的名义写」（进 ACTING）还是「写给谁」（进 COUNTERPARTY 并写理由）？"
        " 在分类之前它被当作 acting 处理。"
    )


def test_counterparty_每条都写了理由() -> None:
    bad = [k for k, v in COUNTERPARTY.items() if len(v.strip()) < 8]
    assert bad == [], f"以下 counterparty 列名没写清为什么不能校验归属：{bad}"


def test_入参带机构标识的写接口都校验了归属() -> None:
    assert RESULT.violations == [], (
        "以下写接口的入参带「以谁的名义写」的机构字段，却不校验调用者能不能以这家"
        "机构的名义写——非全域角色可把业务记到别家账上、以别家名义对外发起、"
        "甚至动别家的实物与人（P1-39）：\n  "
        + "\n  ".join(RESULT.violations)
        + "\n\n修法：函数体里加 `assert_org_writable(db, user, body.<字段>)`。"
    )


def test_缺口只许变少() -> None:
    assert len(RESULT.violations) <= BASELINE_VIOLATIONS, (
        f"未校验归属的写接口从基线 {BASELINE_VIOLATIONS} 涨到 {len(RESULT.violations)}。"
        " 基线只许调小。"
    )


def test_豁免只许变少且每条都有理由() -> None:
    assert len(EXEMPT) == 0, (
        f"本轮手写豁免清零（现 {len(EXEMPT)} 条）；新增豁免须写明为什么该端点"
        "不需要归属校验，且总数只许变少"
    )
    bad = [k for k, v in EXEMPT.items() if len(v.strip()) < 8]
    assert bad == [], f"以下豁免没写清理由：{bad}"


def test_自动豁免的确实只有admin能到() -> None:
    """自动豁免是推导出来的，不是写死的——推导错了会把真缺口放过去。
    这里反向核一遍：被自动豁免的端点，`require_admin` 必须真的在它的依赖链上。"""
    seen: set[int] = set()
    checked = 0
    for r in _walk(app.routes):
        if id(r) in seen or not (r.methods & WRITE):
            continue
        seen.add(id(r))
        key = f"{r.endpoint.__module__}:{r.endpoint.__name__}"
        if key in RESULT.admin_only:
            assert _admin_only(r), f"{key} 被自动豁免，但依赖链上没有 require_admin"
            src = inspect.getsource(r.endpoint)
            assert not any(g in src for g in GUARDS), (
                f"{key} 既有归属校验又被算进自动豁免，两个计数重复了"
            )
            checked += 1
    assert checked == len(RESULT.admin_only) > 0, (
        f"自动豁免核对了 {checked} 个，清单里有 {len(RESULT.admin_only)} 个"
    )


# ---------------------------------------------------------------------------
# P1-56：校验的值必须是服务端事实，不能是调用方自己填的
# ---------------------------------------------------------------------------
#
# 上面那条闸门判的是「写接口调没调 `assert_org_writable`」——**函数名出现在源码里
# 就算过**，不看它校的是什么。于是有一整类写接口能靠一个字段绕过去：请求体里
# 自报一个机构 id 拿去校验（校验当然通过——那是调用方自己的院），同时又按 id
# 引用了一个**自带机构归属**的实体（别家的疫苗批次、别家的住院单、别家的手术室），
# 真正被写的是那个实体所属的机构，而它从头到尾没被看过。
#
# 这条闸门逐个**取数点**判：凡按请求体里的 id 取出一个带 `org_id` 列的实体，
# 函数里就必须拿**这个实体自己的** `.org_id` 去比对或去校验。判据只认绑定名，
# 不认"函数里某处调过守卫"——后者正是上面那条闸门漏掉这一类的原因。
#
# 建闸门时按这个形状在修复前的代码上扫出 **34 处**（P1-56 登记时手工列出的只有 6 处）：
# 26 处补了比对，8 处按业务本就跨机构、逐条写理由豁免。补之前挑了 18 处实打，
# **全部放行**：乙院经办从**甲院住院单退押金** 201、把甲院的住院单结算掉（结算单
# 记在甲院名下）、**订甲院的手术室**、用甲院的疫苗批次接种（扣甲院库存）、
# 在甲院病区收住院、把甲院目标池里的患者划进乙院……行为回归见
# `tests/test_cross_org_body_writes.py`（每条都在修复前的代码上确认过是红的）。

_P56_OWNED: set[str] = set()


def _p56_owned_models() -> set[str]:
    """自带机构归属的模型名——从元数据推导，新表自动进分母。

    P1-59 放宽了两处（原先只认字面 `org_id` 列，于是 `SterilizationBatch.center_org_id`、
    `OrgGroup.lead_org_id` 这些整族不在视野里）：
    - 任一列名以 `org_id` 结尾；
    - **一跳归属**：自己没有机构列、外键指向带机构列的表（里程碑之于项目）。
      指向机构/用户/患者主数据的外键不算——那是"谁经手/关于谁"，不是归属。
    按病种编码挂归属的配置表**不收**：新建时引用它们（引用模板、素材、随访方案）
    是只读引用，不改写配置；它们自己的新建另有一条闸门（见下方 P1-59 段）。
    """
    if not _P56_OWNED:
        classes = [c for c in Base.registry._class_registry.values() if hasattr(c, "__table__")]
        by_table = {c.__tablename__: c for c in classes}
        direct = {
            c.__name__ for c in classes
            if any(col.name.endswith("org_id") for col in c.__table__.columns)
        }
        master = {"organizations", "users", "patients"}
        one_hop = {
            c.__name__ for c in classes
            if c.__name__ not in direct and any(
                fk.column.table.name not in master
                and getattr(by_table.get(fk.column.table.name), "__name__", None) in direct
                for col in c.__table__.columns for fk in col.foreign_keys
            )
        }
        _P56_OWNED.update(direct | one_hop)
    return _P56_OWNED


#: 认作"判过归属"的调用：通用守卫（前两个参数是 db、user），以及同文件里以
#: `_assert` 开头的专用 helper（按单据自己的双方/当前处理方判，如 `_assert_center`）
_P56_GUARDS = {"assert_org_writable", "assert_org_visible", "assert_any_org_writable",
               "assert_obj_org_writable", "assert_program_config_writable"}


def _p56_root(node) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _p56_scan_function(fn, owned: set[str]) -> list[tuple[str, str | None, int, bool]]:
    """一个函数里的 (模型, 绑定名, 行号, 是否比对过它自己的 org_id)。"""
    # 请求体的"根"：`body`，以及 `for item in body.items` 这种从 body 派生的循环变量
    body_roots = {"body"}
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and _p56_root(n.iter) in body_roots:
            body_roots.add(n.target.id)

    parents = {ch: n for n in ast.walk(fn) for ch in ast.iter_child_nodes(n)}

    def bound_name(node) -> str | None:
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Assign):
                target = cur.targets[0]
                return target.id if isinstance(target, ast.Name) else None
            if isinstance(cur, ast.stmt):
                return None  # 如 `if db.get(M, body.x) is None:`——只查存在、不留名，必然没比对
        return None

    fetches = []
    # 父行派生：`parent = db.get(Parent, child.fk)`——判过 parent 就等于判过 child（一跳归属）
    derived: dict[str, set[str]] = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        if n.func.attr == "get" and len(n.args) == 2 and isinstance(n.args[0], ast.Name):
            src = _p56_root(n.args[1])
            name = bound_name(n)
            if name and src and src not in body_roots:
                derived.setdefault(name, set()).add(src)
        if (n.func.attr == "get" and len(n.args) == 2 and isinstance(n.args[0], ast.Name)
                and n.args[0].id in owned and _p56_root(n.args[1]) in body_roots
                and isinstance(n.args[1], ast.Attribute)):
            fetches.append((n.args[0].id, bound_name(n), n.lineno, "get", ast.unparse(n.args[1])))
        elif (n.func.attr == "in_" and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "id" and isinstance(n.func.value.value, ast.Name)
                and n.func.value.value.id in owned and n.args and _p56_root(n.args[0]) in body_roots):
            fetches.append((n.func.value.value.id, bound_name(n), n.lineno, "in_", ""))

    checked: set[str] = set()
    guarded_exprs: list[str] = []
    loops: dict[str, set[str]] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and isinstance(n.iter, ast.Name):
            loops.setdefault(n.iter.id, set()).add(n.target.id)
        if isinstance(n, ast.Compare):
            for side in (n.left, *n.comparators):
                if (isinstance(side, ast.Attribute) and side.attr.endswith("org_id")
                        and isinstance(side.value, ast.Name)):
                    checked.add(side.value.id)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and (
                n.func.id in _P56_GUARDS or n.func.id.startswith("_assert")):
            # 通用守卫的前两个参数是 db、user，判的是后面的；专用 helper 判它收到的实体。
            # 参数里出现的**任何**名字都算判过：`assert_any_org_writable(db, user,
            # (c.from_org_id, c.to_org_id))`、`_assert_center(db, user, specimen)`、
            # `assert_program_config_writable(db, user, rule.program_code)` 都是比对它自己
            judged = n.args[2:] if n.func.id in _P56_GUARDS else n.args
            for a in judged:
                checked.update(x.id for x in ast.walk(a) if isinstance(x, ast.Name))
                guarded_exprs.append(ast.unparse(a))
    # 父行判过 → 子行判过（可传递）
    changed = True
    while changed:
        changed = False
        for parent in list(checked):
            for child in derived.get(parent, ()):
                if child not in checked:
                    checked.add(child)
                    changed = True

    out = []
    for model, name, lineno, kind, id_expr in fetches:
        if kind == "get":
            # 没留名的取数（`if db.get(M, body.x) is None`）也可能被判过：拿**同一个请求体
            # 字段**去取了它的归属再校验（`_group_lead(db, body.org_group_id)`）
            ok = (name is not None and name in checked) or any(
                id_expr in g for g in guarded_exprs if id_expr)
        else:  # 按一组 id 取出来的行：得在逐行循环里比对
            ok = name is not None and bool(loops.get(name, set()) & checked)
        out.append((model, name, lineno, ok))
    return out


def _p56_sites() -> list[tuple[str, str, str, int, bool]]:
    """(文件, 函数, 模型, 行号, 是否比对过)。扫描面：平台与慢专病全部路由文件。"""
    owned = _p56_owned_models()
    app_dir = Path(__file__).resolve().parent.parent / "app"
    files = sorted((app_dir / "routers").rglob("*.py")) + sorted((app_dir / "spd" / "routers").rglob("*.py"))
    out = []
    for path in files:
        rel = path.relative_to(app_dir.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for model, _name, lineno, ok in _p56_scan_function(fn, owned):
                out.append((rel, fn.name, model, lineno, ok))
    return out


#: 按业务本就跨机构的引用，逐条写理由。键 = "文件::函数::模型"。**只减不增。**
P56_CROSS_ORG_BY_DESIGN: dict[str, str] = {
    "app/routers/vaccine_supply.py::report_aefi::VaccinationRecord": (
        "上报机构常常不是接种机构：在乙院打的针、到甲院看的不良反应。接种记录只用来"
        "带出疫苗与批号（且已校验属于同一患者），不改写接种机构的任何数据。"
    ),
    "app/routers/dispense.py::dispense_prescription::Prescription": (
        "县里开方、乡镇取药是医共体的日常。扣的是**发药机构自己**的库存，"
        "发药机构已单独 assert_org_writable；处方只是被引用的依据。"
    ),
    "app/routers/telemedicine.py::reply::Prescription": (
        "引用一张已审通过、且属于同一患者的处方回复咨询，只读引用、不改写处方。"
    ),
    "app/routers/medwaste.py::handover::Employee": (
        "转运可由医共体统一的转运队伍承担，员工档案在此只用于取姓名；医废记录本身"
        "已按本院校验。若业务确认转运人员只能是本院职工，改成比对并删掉这一条。"
    ),
    "app/routers/inpatient.py::create_bed::Ward": (
        "仅 require_admin 可达（admin 是全域角色，比对恒真）。与上面那条闸门的"
        "自动豁免同一理由；require_roles 不享受这条——它会放行自定义角色。"
    ),
    "app/spd/routers/config/teams.py::add_team_member::User": (
        "医共体家医团队本就跨机构组建（县级专科 + 乡镇全科 + 村医）；团队本身已按"
        "团队所属机构校验。按成员所属机构判会挡住正常的团队组建。"
    ),
    "app/spd/routers/tasks.py::assign_task::User": (
        "任务本身已按任务所属机构校验；转派给团队里的上级机构专家是协同本身。"
        "是否要收紧为'只能派给本团队成员'属业务规则，另行确认。"
    ),
    "app/spd/routers/tasks.py::start_path_instance::SpdPathTemplate": (
        "路径模板是配置，引用它不改写模板所属机构的任何数据；纳管档案已按其机构校验。"
    ),
    # ---- P1-59 放宽视野（`*org_id` 列 + 一跳归属）后新看见的，逐条研判为只读引用 ----
    "app/routers/analytics.py::create_outbound_visit::Referral": (
        "县外就诊登记挂一张既有转诊单，只用来算有序转诊率；已校验转诊单属于同一患者，"
        "不改写转诊单。转出/接收机构都可能是登记机构之外的别家，按机构判会漏记有序转诊。"
    ),
    "app/routers/clinical_docs.py::create_nursing_record::InpatientOrder": (
        "医嘱只用来关联，且已比对 `order.admission_id` 等于路径上的住院单——住院单已按其"
        "机构校验，医嘱的归属就是那张住院单的（一跳）。闸门认不出拿路径参数比对这种写法。"
    ),
    "app/routers/exams.py::create_request::ExamRequest": (
        "检查结果互认：医共体搞互认就是要让本院引用别家已出的报告，不做重复检查。"
        "已校验同一患者、同一项目、同一中心类型与 30 天窗口；只读引用，不改写别家的申请单。"
    ),
}


def test_按请求体id引用的实体都比对了它自己的机构():
    """P1-56 的闸门：校验的值须来自服务端事实，不能是调用方自己填的。"""
    bad = sorted(
        f"{rel}:{lineno} {fn} 按请求体 id 取了 {model}，却没比对它自己的 org_id"
        for rel, fn, model, lineno, ok in _p56_sites()
        if not ok and f"{rel}::{fn}::{model}" not in P56_CROSS_ORG_BY_DESIGN
    )
    assert bad == [], (
        "以下写接口按请求体里的 id 引用了别家可能拥有的实体，但只校验了调用方自报的机构：\n  "
        + "\n  ".join(bad)
        + "\n\n修法：取出实体后 `assert_org_writable(db, user, <实体>.org_id)`，"
        "或比对 `<实体>.org_id != body.org_id` 返回 422。确属跨机构协同的，"
        "写进 P56_CROSS_ORG_BY_DESIGN 并说明理由。"
    )


def test_跨机构豁免没有陈旧条目():
    unchecked = {f"{rel}::{fn}::{model}" for rel, fn, model, _l, ok in _p56_sites() if not ok}
    stale = sorted(set(P56_CROSS_ORG_BY_DESIGN) - unchecked)
    assert stale == [], f"这些豁免已经不需要了（已加比对或已删除），请从清单里去掉：{stale}"


def test_跨机构豁免每条都写了理由():
    thin = sorted(k for k, why in P56_CROSS_ORG_BY_DESIGN.items() if len(why.strip()) < 20)
    assert thin == [], f"这些豁免理由太短，说不清为什么该跨机构：{thin}"


def test_P56判据不空转():
    """把修复前的三种形状喂回去，必须全部认出来；修复后的写法必须放行。"""
    owned = {"VaccineBatch", "SpdCandidate", "Department"}
    before = ast.parse(
        "def vaccinate(body, db, user):\n"
        "    assert_org_writable(db, user, body.org_id)\n"
        "    batch = db.get(VaccineBatch, body.batch_id)\n"
        "    if batch is None: raise X\n"
        "def distribute(body, db, user):\n"
        "    assert_org_writable(db, user, body.org_id)\n"
        "    rows = db.query(SpdCandidate).filter(SpdCandidate.id.in_(body.ids)).all()\n"
        "    for c in rows: c.org_id = body.org_id\n"
        "def purchase(body, db, user):\n"
        "    if db.get(Department, body.dept_id) is None: raise X\n"
    )
    flagged = [s for fn in before.body for s in _p56_scan_function(fn, owned) if not s[3]]
    assert len(flagged) == 3, f"修复前的三种形状只认出 {len(flagged)} 种：{flagged}"

    after = ast.parse(
        "def vaccinate(body, db, user):\n"
        "    batch = db.get(VaccineBatch, body.batch_id)\n"
        "    if batch.org_id != body.org_id: raise X\n"
        "def distribute(body, db, user):\n"
        "    rows = db.query(SpdCandidate).filter(SpdCandidate.id.in_(body.ids)).all()\n"
        "    for c in rows: assert_org_writable(db, user, c.org_id)\n"
        "def purchase(body, db, user):\n"
        "    dept = db.get(Department, body.dept_id)\n"
        "    if dept.org_id != body.org_id: raise X\n"
    )
    still = [s for fn in after.body for s in _p56_scan_function(fn, owned) if not s[3]]
    assert still == [], f"修复后的写法被误报：{still}"
    # 反向：`assert_org_writable(db, user, body.org_id)` 这种**校验自报字段**的写法
    # 不能被当成比对过——那正是这一类缺陷本身
    lying = ast.parse(
        "def f(body, db, user):\n"
        "    batch = db.get(VaccineBatch, body.batch_id)\n"
        "    assert_org_writable(db, user, body.org_id)\n"
    )
    assert not _p56_scan_function(lying.body[0], owned)[0][3]


def test_P56覆盖面自证(capsys):
    sites = _p56_sites()
    ok = sum(1 for s in sites if s[4])
    exempt = sum(1 for rel, fn, model, _l, good in sites
                 if not good and f"{rel}::{fn}::{model}" in P56_CROSS_ORG_BY_DESIGN)
    with capsys.disabled():
        print(f"\n  [P1-56] 按请求体 id 取「自带机构」实体的取数点：{len(sites)} 处"
              f"（带 org_id 列的模型 {len(_p56_owned_models())} 个，从元数据推导）")
        print(f"    比对过实体自己的 org_id：{ok}")
        print(f"    按业务跨机构、书面豁免：{exempt}")
        print("    认不出的形态：取数写在 helper 里（按路径 id 的也不在本条射程，归横向越权闸门）")
    assert sites


def test_P59放宽的判据不空转():
    """P1-59 新认的几种"判过"写法各喂一次，修复前的形状必须仍被认出。"""
    owned = {"SterilizationBatch", "OrgGroup", "InpatientOrder", "InpatientAdmission"}
    before = ast.parse(
        # 机构列不叫 org_id：原先根本不在分母里
        "def cost(body, db, user):\n"
        "    if db.get(SterilizationBatch, body.batch_id) is None: raise X\n"
        # 一跳归属：只判了请求体里自报的机构
        "def nurse(body, db, user):\n"
        "    order = db.get(InpatientOrder, body.order_id)\n"
        "    assert_org_writable(db, user, body.org_id)\n"
    )
    flagged = [s for fn in before.body for s in _p56_scan_function(fn, owned) if not s[3]]
    assert len(flagged) == 2, f"修复前的两种形状只认出 {len(flagged)} 种：{flagged}"

    after = ast.parse(
        "def cost(body, db, user):\n"
        "    batch = db.get(SterilizationBatch, body.batch_id)\n"
        "    assert_org_writable(db, user, batch.center_org_id)\n"
        # 没留名，但拿同一个请求体字段取了归属去校验
        "def pool(body, db, user):\n"
        "    if db.get(OrgGroup, body.group_id) is None: raise X\n"
        "    assert_org_writable(db, user, _group_lead(db, body.group_id))\n"
        # 父行判过 → 子行判过
        "def nurse(body, db, user):\n"
        "    order = db.get(InpatientOrder, body.order_id)\n"
        "    adm = db.get(InpatientAdmission, order.admission_id)\n"
        "    assert_org_writable(db, user, adm.org_id)\n"
        # 专用 helper 判它收到的实体
        "def spec(body, db, user):\n"
        "    batch = db.get(SterilizationBatch, body.batch_id)\n"
        "    _assert_center(db, user, batch)\n"
    )
    still = [s for fn in after.body for s in _p56_scan_function(fn, owned) if not s[3]]
    assert still == [], f"修复后的写法被误报：{still}"


# ---------------------------------------------------------------------------
# P1-59：新建"按病种编码挂归属"的配置，必须过病种归属校验
# ---------------------------------------------------------------------------
#
# 量表、宣教素材、服务包、随访方案、干预模板、上报任务……只挂 `program_code`（字符串，
# 不是外键），归属落在病种的牵头机构上（`spd/service.program_lead_org`）。上面两条闸门
# 都看不见这个形状：请求体里没有机构 id，也没有按 id 取出任何带主的实体——于是乙院医师
# 能在甲院牵头的病种下建转诊规则、建服务包（P1-58 实测 201），而闸门一直是绿的。
# 判据：写接口里**构造**了这类模型，就必须调 `assert_program_config_writable`。


def _code_keyed_config_models() -> set[str]:
    """挂 `program_code`、不带 `patient_id`，且没有「以谁的名义」那类机构列的配置表。

    只带 counterparty 机构列的也算（如转诊规则的 `target_org_id`——那是转诊**去向**，
    不是配置归谁），否则它会因为有一个机构列而被当成"自带归属"漏出去。
    """
    return {
        c.__name__ for c in Base.registry._class_registry.values()
        if hasattr(c, "__table__")
        and "program_code" in c.__table__.columns
        and "patient_id" not in c.__table__.columns
        and all(col.name in COUNTERPARTY for col in c.__table__.columns
                if col.name.endswith("org_id"))
    }


#: 构造了这类模型、却按设计不判病种归属的写接口。**只减不增**，每条写理由。
P59_CONFIG_CREATE_OK: dict[str, str] = {
    "app/spd/routers/config/centers.py::create_center::SpdCenter": (
        "专病中心有**自己的**牵头机构（`lead_org_id`，建中心时已按它校验，P1-57），"
        "`program_code` 只说明它是哪个病种的中心。乡镇在县医院牵头的病种下建分中心"
        "是模型本身描述的形态（「分中心运行状态」），按病种牵头机构判会把分中心关掉。"
    ),
    "app/spd/routers/assess.py::run_scoring::SpdScore": (
        "考核结果不是配置：按考核方案对**全部**考核对象（机构/团队/个人）批量算分落库，"
        "`program_code` 只是统计口径的过滤条件，不是归属。考核由卫健侧统一跑，按病种牵头"
        "机构判会让县医院之外谁都跑不了全县考核。"
    ),
}


def _p59_config_creates() -> list[tuple[str, str, str, bool]]:
    models = _code_keyed_config_models()
    app_dir = Path(__file__).resolve().parent.parent / "app"
    files = sorted((app_dir / "routers").rglob("*.py")) + sorted((app_dir / "spd" / "routers").rglob("*.py"))
    out = []
    for path in files:
        rel = path.relative_to(app_dir.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            decs = [ast.unparse(d) for d in fn.decorator_list]
            if not any(m in d for d in decs for m in (".post(", ".put(", ".patch(")):
                continue
            built = {c.func.id for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in models}
            ok = "assert_program_config_writable" in ast.unparse(fn)
            for model in sorted(built):
                out.append((rel, fn.name, model, ok))
    return out


def test_新建病种配置都过了病种归属校验(capsys):
    sites = _p59_config_creates()
    bad = sorted(f"{rel}::{fn}::{model}" for rel, fn, model, ok in sites
                 if not ok and f"{rel}::{fn}::{model}" not in P59_CONFIG_CREATE_OK)
    with capsys.disabled():
        print(f"\n  [P1-59] 新建按病种编码挂归属的配置：{len(sites)} 处"
              f"（此类模型 {len(_code_keyed_config_models())} 个，从元数据推导）")
    assert sites, "一处都没扫到——判据断了（模型集合或构造识别失效），不是没有欠账"
    assert bad == [], (
        "以下写接口新建了挂在病种下的配置，却没过病种归属校验：\n  " + "\n  ".join(bad)
        + "\n\n修法：`assert_program_config_writable(db, user, body.program_code)`"
        "（`spd/service.py`）。确属按设计不判的，写进 P59_CONFIG_CREATE_OK 并说明理由。"
    )
    stale = sorted(set(P59_CONFIG_CREATE_OK) - {f"{r}::{f}::{m}" for r, f, m, ok in sites if not ok})
    assert stale == [], f"这些豁免已经不需要了：{stale}"
