"""写侧越权判据的**第三个盲区**：id 从 body 收的写端点，整族在分母之外。

## 为什么单独立一条，而不是把 `test_stage15_horizontal.py` 的判据放宽

那条判据的第一步就是：

    if not any("{" in d for d in decs):   # 只看路径带 {} 的端点
        continue

于是 `POST /api/mgmt/secondments`（`employee_id` 在 body 里）这类端点**从来没进过分母**
——不是"查过没问题"，是"没被数到"。该文件自己的注释早就写明判据"比缺陷窄两层"，
并列了两个同形状的历史案例（P1-42 `distribute_candidates`、P1-47 `batch_tasks`），
两次都是**靠人手工发现**、然后给那一个形状单独补一条用例，判据本身一直没动。

2026-09-18 撞上第三次（P1-59）：实测乙院 operator 能给甲院医师建派驻、签劳动合同，
两条都 201 落库。**同一个盲区被撞第三次，就不该再靠下一个人碰巧发现。**

直接放宽原判据会把那条棘轮的语义搞混（它的基线是"已逐条判定完、零欠账"，
而这里是一批**尚未逐条判定的候选**）。所以另起一条，语义清楚：
**这是候选清单，不是缺陷清单，只许变少。**

## 判据给的是候选，不是缺陷——这一条必须写在最前面

本仓库有过教训：`spd/referral` 五条曾被判"无守卫"，逐条取证后发现**本就有守卫**，
是判据误报。本清单同理，已知至少两类不是缺陷：

* `billing.py:payment_callback` —— 支付网关回调，**按设计没有调用方身份**
  （HMAC 验签 + 时间戳窗口 + 与本地单核对金额），下面写成豁免。
* `appointments.py:book` —— 挂号本就跨机构（便捷寻医、转诊），
  "能给别家机构的号源下单"多半是功能不是洞，但要产品确认，暂留在候选里。

所以：**名单变短的正当方式有两种**——补上守卫，或逐条取证后判为按设计并移入豁免
（豁免必须写明理由）。不许只因为"看着像没问题"就划掉。
"""
from __future__ import annotations

import ast
import pathlib

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"

#: 与 `test_stage15_horizontal.py` 同一套守卫名（那边改了这边要跟上，
#: 故这里显式再列一份并有用例比对两边一致，不靠"记得同步"）。
GUARDS = {
    "assert_obj_org_writable", "assert_org_writable", "assert_org_visible",
    "assert_patient_visible", "scope_org_list", "scope_patient_list", "log_patient_access",
}

#: **按设计没有调用方身份**的写端点，逐条写明理由。
EXEMPT = {
    # 支付网关回调：调用方是网关不是人，没有 user 可判。安全性由 HMAC 验签
    # （`verify_signature`）+ 时间戳重放窗口 + 与本地订单核对金额三道承担；
    # 密钥未配置时整条端点 503 不可用。给它加机构守卫既无从下手也无意义。
    "billing.py:payment_callback",
}

#: **角色门只允许全域角色**（`visibility.GLOBAL_ROLES = {"admin", "director"}`）的写端点。
#: 与 `EXEMPT` 分开列，因为理由不同：那些是"按设计没有调用方身份"，这些是
#: "有身份，但能调它的角色本来就跨机构"。
#:
#: **不是可越权入口**：非全域角色连角色门都过不去。给它们补归属校验只是纵深防御，
#: 当成洞去修会写出触发不了的"漏洞修复"——本轮差点就这么干了
#: （`admin_mgmt.create_payroll` 那次已经踩过一回，见 P1-59 的更正）。
#: 角色门若哪天放宽到非全域角色，`tests/test_clinical_write_org_guard.py` 末尾
#: 那条用例会红，提醒把它挪回候选。
GLOBAL_ROLE_ONLY = {
    "appointments.py:create_slot",       # require_admin
    "inpatient.py:create_bed",           # require_admin
    "cost.py:upsert_department_cost",    # require_roles("director")
    "cost.py:create_allocation_rule",    # require_roles("director")
}

#: 候选清单（**不是缺陷清单**，见模块 docstring）。只许变少。
#: 划掉的正当方式：① 补守卫；② 逐条取证判为按设计后移入 `EXEMPT` 并写明理由。
#:
#: ✅ 已清：`admin_mgmt.py:second_employee` / `create_staff_contract`（P1-59，实测
#: 乙院 operator 各 201 落库，已补 `assert_obj_org_writable`）；
#: `admin_mgmt.py:create_payroll`（同批补了纵深防御）；
#: `staffing.py:create_secondment`（同一张 secondments 表的另一条 create，
#: ADR-0024 记过"两条 create 都没有归属校验"，实测 201，已补）。
#:
#: ✅ **钱那一族 5 条已清（2026-09-18，P1-60 第一批）**：`billing` 的计费明细 /
#: 押金收 / 押金退 / 出院结算 / 收款。实测乙院 operator 对甲院的一次住院把
#: **进账、出账、结算、收款整条资金链走完**，五条全部 201；五条的角色门都是
#: `require_roles("operator")`，而 operator 不在 GLOBAL_ROLES 里，所以是实打实够得着的。
#: 归属分别取自 `Admission.org_id` / `Encounter.org_id` / `Settlement.org_id`；
#: 退费与结算那两句守在临界区之前（钱出去与冲抵押金都在里头）。
#: 回归见 `tests/test_billing_org_guard.py`，变异后五条越权反例各自转红。
#:
#: ✅ **临床那一族 4 条已清（2026-09-18，P1-60 第二批）**：入院登记 / 医嘱开立 /
#: 手术申请 / 交接班。实测乙院 doctor 能把患者**收进甲院病区并占掉床**、
#: 给甲院的住院病人**开长期医嘱**与**申请手术**、给甲院病区写交接班（且回执里的
#: `patient_count` 是按甲院在院数现算的）。钱那一族丢的是账，这一族丢的是
#: **诊疗行为的归属**。归属取自 `Ward.org_id` / `Admission.org_id`。
#: 转诊与会诊都有各自的入口，不是"直接写别家的医嘱单"。
#: 回归见 `tests/test_clinical_write_org_guard.py`，变异后四条越权反例各自转红。
KNOWN_BODY_ID_WRITES: set[str] = {
    "spd/tasks.py:start_path_instance",   # 连角色门都没有：任何已登录账号都能调
    "appointments.py:book",               # 多半按设计跨机构，待产品确认后移入 EXEMPT
    "medwaste.py:collect",
}

_HTTP_VERBS = ("get", "post", "put", "patch", "delete")


def _org_scoped_models() -> set[str]:
    from app import models

    return {
        c.__name__
        for c in models.Base.registry._class_registry.values()
        if hasattr(c, "__tablename__") and "org_id" in c.__table__.columns
    }


def _candidates() -> set[str]:
    """无路径参数、body 收 id、`db.get(带 org_id 的模型, …)`、且没有任何守卫的写端点。"""
    import test_stage15_horizontal as H

    direct = _org_scoped_models()
    out = set()
    for name, path in H._router_files():
        # 居民端两个文件走 portal 令牌 + accessible_patient，不在员工机构可见性体系内，
        # 与上游判据同一理由豁免。
        if name in ("portal.py", "spd/portal.py"):
            continue
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            decs = [ast.unparse(d) for d in fn.decorator_list]
            if not decs or any("{" in d for d in decs):
                continue  # 带路径参数的归上游那条判据管，这里只补它看不见的那一半
            # 剥 docstring 再匹配守卫名：散文里提一句守卫名就能冒充守卫（§8 红线）。
            u = H._with_local_helpers(tree, fn)
            if not H._is_write_endpoint(decs, u):
                continue
            if any(g in u for g in GUARDS):
                continue
            if not any(f"db.get({m}," in u for m in direct):
                continue
            out.add(f"{name}:{fn.name}")
    return out


def test_覆盖面自证():
    """防空转：扫描面与模型面都不能是空的，否则这条闸门什么也没守。"""
    import test_stage15_horizontal as H

    files = H._router_files()
    models = _org_scoped_models()
    cands = _candidates()
    print(
        f"\n[body 收 id 的写侧越权候选] 扫描 {len(files)} 个路由文件、"
        f"{len(models)} 个带 org_id 的模型；候选 {len(cands)} 处"
        f"（豁免 {len(EXEMPT)}、只有全域角色够得着 {len(GLOBAL_ROLE_ONLY)}、"
        f"登记 {len(KNOWN_BODY_ID_WRITES)}）"
    )
    assert len(files) >= 60, f"只扫到 {len(files)} 个路由文件"
    assert len(models) >= 50, f"只认出 {len(models)} 个带 org_id 的模型"


def test_守卫名单与上游判据保持一致():
    """两边各写一份守卫名，必须逐字相同——否则上游加了新守卫，这边会把它当没守卫。"""
    import inspect

    import test_stage15_horizontal as H

    src = inspect.getsource(H._byid_org_write_endpoints)
    for g in GUARDS:
        assert g in src, f"本文件认的守卫 {g!r} 在上游判据里不存在，两边口径已经分叉"


def test_不得新增body收id的无守卫写端点():
    new = sorted(_candidates() - KNOWN_BODY_ID_WRITES - EXEMPT - GLOBAL_ROLE_ONLY)
    assert new == [], (
        "以下写端点从 body 收 id 取到带 org_id 的对象，却没有任何机构归属守卫——\n"
        "角色守卫只回答'谁能做'，回答不了'能对谁做'：\n  " + "\n  ".join(new)
        + "\n\n补 assert_org_writable / assert_obj_org_writable；"
        "若按设计确实没有调用方身份，移入 EXEMPT 并写明理由。"
    )


def test_名单只许变少():
    stale = sorted((KNOWN_BODY_ID_WRITES | EXEMPT | GLOBAL_ROLE_ONLY) - _candidates())
    assert stale == [], (
        "这些已经补上守卫（或已不存在）了，请从名单里划掉，"
        "否则名单会永远停在今天的数字：\n  " + "\n  ".join(stale)
    )


def test_已修各批确实已脱离候选():
    """已修的那几批**必须**不在候选里，否则这份用例在空转。"""
    cands = _candidates()
    for entry in (
        # P1-59 + 盲区棘轮那一批
        "admin_mgmt.py:second_employee",
        "admin_mgmt.py:create_staff_contract",
        "admin_mgmt.py:create_payroll",
        "staffing.py:create_secondment",
        # P1-60 第一批：钱
        "billing.py:create_bill_detail",
        "billing.py:create_deposit",
        "billing.py:refund_deposit",
        "billing.py:create_settlement",
        "billing.py:create_payment",
        # P1-60 第二批：临床
        "inpatient.py:create_admission",
        "inpatient.py:create_order",
        "surgery.py:create_request",
        "clinical_docs.py:create_handover",
    ):
        assert entry not in cands, entry
        assert entry not in KNOWN_BODY_ID_WRITES, entry
