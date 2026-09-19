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

import inspect
import warnings

from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.database import Base
from app.main import app

WRITE = {"POST", "PUT", "PATCH", "DELETE"}
GUARDS = ("assert_org_writable", "assert_obj_org_writable")

#: 「以谁的名义写」——必须校验归属
ACTING = {"org_id", "from_org_id", "initiator_org_id", "current_org_id"}
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
