"""收不了口的患者维度读接口：**连调用方身份都没有**，却吐个体身份字段。

## 这个形状是什么

一个 GET 端点如果**没有任何形参绑到调用方身份**
（`Depends(get_current_user)` / `current_resident` / `current_resident_patient`），
那它**在结构上就不可能**按归属收口——没有"谁在问"，就无从判断"他能不能看"。
这类端点若同时吐出个体身份字段（`patient_id`/`patient_name`/`ehc_no`/`phone`/
`birth_date`/`name`），任何登录账号翻页就能把全域名单拉走。

横向越权闸门（`test_stage15_horizontal.py`）问的是**按 id 直取**那一族，
分母是「入参含患者标识」＋「`db.get(带 patient_id 的模型, …)`」。
**列表族整个不在它的分母里**——本条补的正是这一格。

## 为什么只登记、不在这里修

改法要回答的是业务问题：这份名单**该给谁看**。
质控抽样、结果互认、集中审方这些恰恰是**按设计跨机构**的（横向越权闸门里已有
`recognition` / `prescriptions:prescription_review_points` 这类带书面理由的豁免）；
而群组成员、服务申请、外呼任务显然不该全域可见。逐条判定属业务裁定，
与 `docs/待裁定事项清单.md` 里 P1-49 同一性质，故**登记不擅改**。

本用例要做的只有一件事：**把这个缺口从"看不见"变成"数得出、只减不增"**。

## 判据是语义的，不是拼写的

第一版按形参**名字**判（有没有叫 `user` 的形参），当场被
`access_logs:my_access_logs` 打脸——它写的是
`patient: Patient = Depends(current_resident_patient)`，确确实实收了口，却被误报。
本轮审别人的毛病正是"判据只认一种写法"，自己先犯了一次。
现在按**绑没绑到身份依赖**判。
"""
import ast
import importlib
import os
import pathlib
import sys

import astcode

SERVER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

#: 解析"调用方是谁"的依赖。绑了其中任一个，就有收口的可能（本条不再追问它有没有收）。
IDENTITY_DEPS = ("get_current_user", "current_resident", "current_resident_patient")

#: 算作"个体身份"的响应字段。
IDENTITY_FIELDS = {
    "patient_id", "patient_name", "ehc_no", "id_card", "phone", "name", "birth_date",
}

#: 【判据的分母，说清它看得见什么】"患者维度模型" = ORM 里**任一列名以 `patient_id`
#: 结尾**的表（外加 `Patient` 本身）。
#:
#: ⚠️ 第一版只认列名**恰好等于** `patient_id`，当场漏掉 `child_records`——它的列叫
#: `guardian_patient_id`（监护人）。而挂在它上面的 `maternal:list_high_risk_children`
#: 恰恰是 `docs/待裁定事项清单.md` 里被单独点名"这一族里风险最集中"的那一个：
#: 儿童姓名 + 出生日期 + 高危原因，端点级无 `require_roles`。
#: 放宽后从 33 涨到 36，多出来的正是它与 `maternal:list_children`、
#: `materials:trace_consumable`（高值耗材植入追溯）。
#: **判据只认一种写法，第三次在同一天咬人。**
#:
#: 仍看不见的：**完全不带 `patient_id` 类外键的人物表**（例如只有姓名+证件号的名册），
#: 这一格本条守不住，须靠 P1-35 那边把"归属未定义的表"逐张裁完。
#:
#: 【欠账，只减不增】无调用方身份 × 响应带个体身份。
#: 给某个端点补上身份依赖与归属收口之后，把它从这里删掉。
#:
#: ⚠️ 其中三个吐的是**联系方式与证件号**，不只是 patient_id——
#: `spd/followup.py:list_call_tasks`（phone）、
#: `spd/population.py:list_group_members` 与 `:list_service_applies`
#: （phone + ehc_no + birth_date + name）。明文电话那一面已另行登记 P1-39。
UNSCOPABLE_PATIENT_READS = {
    "analytics.py:list_outbound_visits",
    "appointments.py:list_blacklist",
    "checkups.py:abnormal_checkups",
    "chronic.py:list_chronic",
    "chronic.py:list_overdue",
    "consents.py:list_corrections",
    "consultations.py:list_consultations",
    "credentials.py:lookup",
    "credentials.py:resolve_any",
    "disease_programs.py:program_stats",
    "eldercare.py:disabled_elderly",
    "eldercare.py:eldercare_alerts",
    "emergency.py:list_cases",
    "exams.py:list_requests",
    "followups.py:overdue_followups",
    "insurance.py:list_dual_channel",
    "insurance.py:list_special_diseases",
    "materials.py:trace_consumable",
    "maternal.py:list_children",
    "maternal.py:list_high_risk_children",
    "maternal.py:list_records",
    "medication.py:list_shortages",
    "outpatient_docs.py:list_treatments",
    "prescriptions.py:list_prescriptions",
    "quality.py:clinical_indicators",
    "referrals.py:list_referrals",
    "spd/care.py:list_edu_pushes",
    "spd/followup.py:list_call_tasks",
    "spd/followup.py:list_qc_samples",
    "spd/population.py:list_group_members",
    "spd/population.py:list_lifecycle_events",
    "spd/population.py:list_service_applies",
    "spd/tasks.py:get_path_instance",
    "surveys.py:list_surveys",
    "tcm.py:list_orders",
    "telemedicine.py:list_consults",
}

#: 同一形状、但响应只有聚合/计数，没有个体身份。**信息项**，不是欠账。
#: 单列出来是为了让上面那份清单的分母有对照——没有它，
#: "33"这个数字读起来像是"全部无身份读接口"，其实不是。
AGGREGATE_ONLY_READS = {
    "analytics.py:patient_flow",
    "consultations.py:consultation_stats",
    "eldercare.py:eldercare_stats",
    "followups.py:followup_stats",
    "maternal.py:list_screenings",
    "prescriptions.py:prescription_review_points",
    "spd/care.py:assessment_stats",
    "spd/care.py:edu_stats",
    "spd/config/devices.py:list_devices",
    "surveys.py:survey_stats",
}


def _router_files() -> list[tuple[str, str]]:
    out = []
    for base, label in (("app/routers", ""), ("app/spd/routers", "spd/")):
        root = os.path.abspath(SERVER / base)
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            rel = os.path.relpath(d, root)
            pre = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for n in sorted(x for x in names if x.endswith(".py")):
                out.append((f"{label}{pre}{n}", os.path.join(d, n)))
    return sorted(out)


def _binds_identity(fn: ast.AST) -> bool:
    defaults = list(fn.args.defaults or []) + [
        d for d in (fn.args.kw_defaults or []) if d is not None
    ]
    return any(
        f"Depends({dep})" in ast.unparse(d) for d in defaults for dep in IDENTITY_DEPS
    )


def _response_fields(mod, name: str | None, depth: int = 0) -> set[str]:
    if depth > 4 or name is None or mod is None:
        return set()
    obj = getattr(mod, name, None)
    if obj is None or not hasattr(obj, "model_fields"):
        return set()
    out: set[str] = set()
    for field, info in obj.model_fields.items():
        out.add(field)
        ann = str(info.annotation)
        for sub in dir(mod):
            nested = getattr(mod, sub, None)
            if sub != name and sub in ann and hasattr(nested, "model_fields"):
                out |= _response_fields(mod, sub, depth + 1)
    return out


def _scan() -> tuple[set[str], set[str]]:
    """返回 (带个体身份的, 仅聚合的)。"""
    from app import models
    from app.main import app  # noqa: F401  触发全部路由 import（含 spd）

    linked = {
        c.__name__
        for c in models.Base.registry._class_registry.values()
        if hasattr(c, "__tablename__")
        and any(col.endswith("patient_id") for col in c.__table__.columns.keys())
    } | {"Patient"}

    ident: set[str] = set()
    agg: set[str] = set()
    for name, path in _router_files():
        # 居民端另一套鉴权（portal 令牌只看自己），与横向越权闸门同一理由豁免
        if name in ("portal.py", "spd/portal.py"):
            continue
        modname = (
            f"app.routers.{name[:-3]}" if not name.startswith("spd/")
            else f"app.spd.routers.{name[4:-3]}"
        ).replace("/", ".")
        try:
            mod = importlib.import_module(modname)
        except Exception:      # noqa: BLE001 - 扫描不该因单个模块导入失败而中断
            mod = None
        for fn in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = " ".join(ast.unparse(d) for d in fn.decorator_list)
            if ".get(" not in decs or _binds_identity(fn):
                continue
            body = astcode.code(fn)
            if not any(f"db.query({m})" in body or f"db.get({m}," in body for m in linked):
                continue
            model_name = None
            for d in fn.decorator_list:
                s = ast.unparse(d)
                if "response_model=" in s:
                    model_name = (
                        s.split("response_model=")[1].split(",")[0]
                        .strip(") ").replace("list[", "").replace("]", "").strip()
                    )
            fields = _response_fields(mod, model_name)
            (ident if fields & IDENTITY_FIELDS else agg).add(f"{name}:{fn.name}")
    return ident, agg


def test_收不了口的患者读接口只减不增():
    """棘轮：这份清单只许变短。

    **自证覆盖面**：扫了多少文件、当前多少条，一并打印——
    一个不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险。
    """
    ident, agg = _scan()
    print(
        f"\n[无身份患者读接口] 覆盖面自证\n"
        f"  扫描文件：{len(_router_files())} 个（app/routers + app/spd/routers，递归，"
        f"居民端两个文件按 portal 令牌另计）\n"
        f"  无调用方身份 × 触达患者维度模型：{len(ident) + len(agg)} 个\n"
        f"    其中响应带个体身份：{len(ident)} 个（基线 {len(UNSCOPABLE_PATIENT_READS)}）\n"
        f"    其中仅聚合无身份：{len(agg)} 个（信息项，非欠账）\n"
        f"  说明：没有形参绑到 {IDENTITY_DEPS} 中任一个 ⇒ 结构上无从判断"
        f"「这个调用方能不能看这个患者」。逐条该给谁看属业务裁定，见 docs/待裁定事项清单.md。"
    )
    new = sorted(ident - UNSCOPABLE_PATIENT_READS)
    assert new == [], (
        "以下 GET 端点没有任何调用方身份依赖（结构上无法按归属收口），"
        "响应里却带个体身份字段：\n  " + "\n  ".join(new)
        + "\n请补 `user: User = Depends(get_current_user)` 并接 visibility 收口；"
        "\n若按设计就要跨机构（结果互认 / 集中审方一类），"
        "请登记进 UNSCOPABLE_PATIENT_READS 并写明理由。"
    )


def test_清单不得腐烂():
    """登记项必须仍然命中——端点改名或补了收口之后，清单要跟着变短。"""
    ident, _ = _scan()
    stale = sorted(UNSCOPABLE_PATIENT_READS - ident)
    assert stale == [], (
        f"这些登记项已不再命中（改名、删除，或已补上身份依赖），应从清单删掉：{stale}"
    )


def test_判据按身份依赖判而不是按形参名判():
    """防的是第一版那个错：按形参叫不叫 `user` 判。

    `access_logs:my_access_logs` 收的是
    `patient: Patient = Depends(current_resident_patient)`——名字不叫 user，
    却确确实实绑了调用方身份。按名字判会把它误报成"收不了口"。
    """
    named_user = ast.parse(
        "def f(user: User = Depends(get_current_user)):\n    pass\n"
    ).body[0]
    named_patient = ast.parse(
        "def f(patient: Patient = Depends(current_resident_patient)):\n    pass\n"
    ).body[0]
    no_identity = ast.parse(
        "def f(db: Session = Depends(get_db)):\n    pass\n"
    ).body[0]
    assert _binds_identity(named_user)
    assert _binds_identity(named_patient), "按形参名判会漏掉这一种——第一版就是这么错的"
    assert not _binds_identity(no_identity)


def test_两份清单不重叠且都不为空():
    """防空转：两份清单一空，上面两条就都成了永远为真的规则。"""
    assert UNSCOPABLE_PATIENT_READS, "欠账清单空了？那这条棘轮什么也没守"
    assert AGGREGATE_ONLY_READS, "对照清单空了——分母没了参照，数字会被误读"
    overlap = UNSCOPABLE_PATIENT_READS & AGGREGATE_ONLY_READS
    assert overlap == set(), f"同一端点同时进了两份清单，分类逻辑坏了：{sorted(overlap)}"
