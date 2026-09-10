"""PII 出口脱敏守卫（P1-33）：响应里带身份证号/电话的端点，函数体必须走 `privacy` 的脱敏入口。

## 为什么要有这一条

`app/privacy.py` 第 7 行写着"新增返回身份证号/电话的接口必须复用本模块，禁止各自实现"。
写下这句话时全仓库只有 2 处引用、没有任何检查在证明它——这正是 CLAUDE.md §8
"出口一律经 privacy.py 脱敏"那条红线**靠人记得**的形态。本轮盘点出来的第一个洞就是这么
漏的：`POST /api/patients` 幂等命中既有档案时把**别人录入的**那份原样返回，一个只知道
证件号的账号借"建档"就能把电话套出来（现已改走 `desensitize`，行为回归在文件末尾）。

## 判定（从路由对象推导，不手写分母）

分母：`app/routers` 与 `app/spd/routers` 里每个 APIRoute 的 `response_model` **递归**展开
（list / Optional / 嵌套模型都进），字段名是 `id_card`/`phone` 或以 `_id_card`/`_phone`
结尾的，就是"带 PII 的出口"。契约治理已到 100%（`test_api_contract_governance.py`），
所以这个分母是全的；裸 `dict`/`Any` 契约看不出字段名，其数量在盘点里打印。

三种"合规"：

1. **已脱敏**：端点函数体，或它调用的路由包内帮手函数（跨模块也跟，最多三层），
   出现 `desensitize` / `mask_id_card` / `mask_phone` 之一。按调用链追而不是只看函数体，
   是因为现状的正确写法多半在 `_out()` 帮手里（`consent_out`、`_death_card`、
   `_do_fhir_patient`）——只看函数体会把它们全判成漏，规则一上来就是一串假红。
   只追路由包内的函数：追到 `sms.py` 那种"日志里把号码掩掉"的帮手会误判成"响应已脱敏"。
2. **居民本人侧**（`/api/portal/`）：privacy.py 明写"居民本人侧可返回本人明文"——本人看
   自己的号码不是泄露；家庭代管读到的成员号码，绑定时已用该号码收过验证码（P1-2）。
3. **书面登记的明文出口**：`ACCEPTED_PLAINTEXT_EGRESS`，逐条写理由，**只减不增**，且条目
   必须仍然成立（端点还在、还带 PII、还没脱敏），否则由防腐烂用例逼着删掉。

## 认不出的形态（如实声明）

帮手以方法形式调用（`svc.mask()`）不解析；掩码若写在 Pydantic validator / serializer 里
也认不出（现状没有这种写法，有了会先在这里变红、再来放行）。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import textwrap
import typing

from fastapi import APIRouter
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute
from pydantic import BaseModel

import app.routers as platform_routers
import app.spd.routers as spd_routers
from app.main import app  # noqa: F401  确保全部路由模块（含 spd 子包）已 import
from app.privacy import mask_id_card, mask_phone
from conftest import login

PII_FIELD_NAMES = ("id_card", "phone")
MASKERS = frozenset({"desensitize", "mask_id_card", "mask_phone"})
RESIDENT_SIDE_PREFIXES = ("/api/portal/",)
ROUTER_PACKAGES = (platform_routers.__name__ + ".", spd_routers.__name__ + ".")
MAX_CALL_DEPTH = 3

#: 书面登记的明文出口：`文件:函数 → 理由`。只减不增；条目一旦不再成立（端点没了 /
#: 不再带 PII / 已改走脱敏），`test_明文出口登记不得腐烂` 会要求删掉它。
ACCEPTED_PLAINTEXT_EGRESS: dict[str, str] = {
    # —— 120 调度：呼救人回拨号码 ——
    # 呼救人常常不是患者本人；调度员按号码回拨核实地址、指引现场急救是业务主链，掩码后
    # 无法回拨。端点仍受 require_roles 限制。是否改为按角色掩码属产品口径（另案）。
    "emergency.py:dispatch": "120 调度台回拨呼救人（呼救人非患者本人，回拨是调度主链）",
    "emergency.py:list_cases": "120 调度台回拨呼救人（同上）",
    "emergency.py:advance": "120 调度台回拨呼救人（同上，推进状态回显同一模型）",
    "emergency.py:set_rescue_outcome": "120 调度台回拨呼救人（同上，抢救结论回显同一模型）",
    # —— 村医通讯录：工作人员联系方式，不是居民 PII（H1 的范围是居民身份证号/电话）——
    "spd/config/teams.py:create_village_doctor": "村医通讯录，工作人员联系方式而非居民 PII",
    "spd/config/teams.py:list_village_doctors": "村医通讯录，工作人员联系方式而非居民 PII",
    "spd/config/teams.py:update_village_doctor": "村医通讯录，工作人员联系方式而非居民 PII",
    # —— 慢专病服务团队工作台：行内带在管患者电话 ——
    # 电话随访与外呼默认走 ManualCallProvider **人工拨号**，掩码后随访无法执行；spd 子系统
    # 建时未纳入 H1 口径。是否按角色掩码（如仅 director 看全量）属产品裁定，见 TECH_DEBT
    # P1-33 后续项；在裁定之前登记在此，不许再新增同类出口。
    "spd/followup.py:create_call_task": "外呼任务回执带拨号号码（ManualCallProvider 人工拨号）",
    "spd/followup.py:list_call_tasks": "呼叫任务列表带拨号号码（同上）",
    "spd/followup.py:followup_context": "随访前置资料（电话随访前一屏看到号码）",
    "spd/population.py:list_candidates": "候选池行内电话（服务团队联系纳管）",
    "spd/population.py:claim_candidate": "候选池认领回执（同一模型）",
    "spd/population.py:set_candidate_status": "候选池状态回执（同一模型）",
    "spd/population.py:create_enrollment": "在管档案回执（同一模型）",
    "spd/population.py:list_enrollments": "在管档案列表行内电话（服务团队随访联系）",
    "spd/population.py:get_enrollment": "在管档案详情（同上）",
    "spd/population.py:update_enrollment": "在管档案更新回执（同一模型）",
    "spd/population.py:lifecycle_event": "生命周期事件回执（同一模型）",
    "spd/population.py:confirm_migration": "迁出确认回执（同一模型）",
    "spd/population.py:list_group_members": "分组成员行内电话（批量干预/联系）",
    "spd/population.py:patient_profile": "患者 360 档案（服务团队随访联系）",
    "spd/population.py:list_service_applies": "居民服务申请留的回访电话（团队回访）",
    "spd/tasks.py:list_tasks": "任务中心行内患者电话（电话随访类任务）",
    "spd/tasks.py:get_task": "任务详情患者电话（同上）",
}


# --------------------------------------------------------- 分母：从路由对象推导


def _module_key(module_name: str) -> str:
    """`app.routers.x` → `x.py`；`app.spd.routers.config.teams` → `spd/config/teams.py`。"""
    for prefix, label in ((ROUTER_PACKAGES[1], "spd/"), (ROUTER_PACKAGES[0], "")):
        if module_name.startswith(prefix):
            return label + module_name[len(prefix):].replace(".", "/") + ".py"
    return module_name


def _iter_routes():
    """两个路由包（含子包）里的全部 APIRoute，按对象去重（config 子包多处 import 同一个 router）。"""
    seen: set[int] = set()
    for pkg in (platform_routers, spd_routers):
        for modinfo in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            module = importlib.import_module(modinfo.name)
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if not isinstance(route, APIRoute) or id(route) in seen:
                        continue
                    if not (route.methods - {"HEAD", "OPTIONS"}):
                        continue
                    seen.add(id(route))
                    yield route


def _is_pii_name(name: str) -> bool:
    low = name.lower()
    return any(low == pii or low.endswith("_" + pii) for pii in PII_FIELD_NAMES)


def _pii_fields(tp, seen: set | None = None) -> list[str]:
    """响应模型里（递归）名字像 PII 的字段：`模型.字段`。"""
    seen = set() if seen is None else seen
    if typing.get_origin(tp) is not None:
        return [f for arg in typing.get_args(tp) for f in _pii_fields(arg, seen)]
    if not (inspect.isclass(tp) and issubclass(tp, BaseModel)) or tp in seen:
        return []
    seen.add(tp)
    found = []
    for name, field in tp.model_fields.items():
        if _is_pii_name(name):
            found.append(f"{tp.__name__}.{name}")
        found.extend(_pii_fields(field.annotation, seen))
    return found


def _is_loose_contract(tp) -> bool:
    """看不出字段名、且值类型装得下字符串的契约：裸 `dict`、`Any`、`dict[str, str|Any]`。

    `dict[str, int]` 这类值类型是数字的不算——装不下证件号与电话，不是盲区。
    """
    origin = typing.get_origin(tp) or tp
    if origin is typing.Any:
        return True
    if not (inspect.isclass(origin) and issubclass(origin, dict)):
        return False
    args = typing.get_args(tp)
    if len(args) != 2:
        return True  # 裸 dict：值类型未知
    value_type = args[1]
    return value_type not in (int, float, bool)


#: 值类型装得下字符串的裸契约里，经人工复核**确实不含**居民身份证号/电话的端点：
#: `文件:函数 → 理由`。守卫看不进这类契约，只能逐个复核登记；同样只减不增、不得腐烂。
LOOSE_CONTRACT_REVIEWED: dict[str, str] = {
    "users.py:list_roles": "角色代码 → 角色名称的固定映射，不含任何居民数据",
}


# ------------------------------------------------- 第二个结构性盲区：非 JSON 导出

#: 构造「带响应体的非 JSON 响应」的调用名。**裸 `Response(status_code=204)` 不算**
#: ——它没有响应体，出不了 PII（`spd` 那几个 delete/remove 就是这种）。
_EXPORT_CTOR = re.compile(
    r"\b(StreamingResponse|HTMLResponse|PlainTextResponse|FileResponse"
    r"|CsvResponse|NdjsonResponse|SvgResponse|AttachmentContentResponse"
    r"|_csv_response)\s*\(|\bResponse\s*\(\s*content\s*="
)


def _is_export_route(route) -> bool:
    """这个端点吐的是非 JSON 的响应体（CSV / NDJSON / HTML / SVG / 文件流）吗？

    **两个判据缺一不可**，这一点是实测出来的：

    - 只看 `response_class`：漏掉 `certs.py:export_death_report_cards_csv` 与
      `infectious.py:export_case_report_cards_csv`——它们**没有声明 response_class**，
      只是函数里 `return _csv_response(...)`；
    - 只看函数源码：漏掉 `printing.py` 那 12 个——它们声明了 `response_class=HTMLResponse`，
      而 HTML 是共用帮手 `_page()` 组装的，端点自己的源码里没有构造调用。

    单独一个判据只认得 8 个，两个并起来才是 20 个。
    """
    if not isinstance(route.response_class, DefaultPlaceholder):
        return True
    try:
        src = textwrap.dedent(inspect.getsource(route.endpoint))
    except (OSError, TypeError):
        return False
    return bool(_EXPORT_CTOR.search(re.sub(r'"""(?:.|\n)*?"""', "", src)))


#: 非 JSON 导出里，经人工复核**确实不含**居民身份证号/电话的端点：`文件:函数 → 理由`。
#: 与上面两张清单同一纪律：逐条写理由、只减不增、不得腐烂。
EXPORT_NO_PII_REVIEWED: dict[str, str] = {
    "attachments.py:download_attachment":
        "回放的是上传上来的**文件字节流**（content_type 原样透传），不是平台组装的结构化字段；"
        "里面有什么由上传方决定，掩码无从下手，访问控制由附件归属校验承担",
    "infectious.py:export_case_report_cards_csv":
        "列头是 卡片编号/报告机构/病种编码/病种名称/分类/发病日期/报告时间/法定时限/迟报天数/及时性，"
        "**不含姓名、身份证号、电话**；与同为法定上报的死因卡不同——那份带姓名与证件号，走 _death_card 掩码",
    "reports.py:export_monitoring_csv":
        "监测指标聚合（序号/指标名/口径/当期值/单位/数据来源），无个体数据",
    "reports.py:export_operations_csv":
        "机构维度运营月报（就诊/住院/收入/支出/结余/绩效分），无个体数据",
    "spd/config/scales.py:scale_qr": "量表二维码 SVG，内容是一条 URL",
    "spd/config/teams.py:village_doctor_qr": "村医二维码 SVG，内容是一条 URL",
    "users.py:export_audit_logs":
        "NDJSON 每行只含 id/user_id/username/method/path/status_code/at。"
        "path 取自 `request.url.path`（**不含 query**），且审计只记 _AUDITED_METHODS"
        "（POST/PATCH/PUT/DELETE）；全仓把 ehc_no 放进路径模板的三个端点（/api/archive/{ehc_no}、"
        "/api/patients/{ehc_no}、FHIR Patient/{ehc_no}）都是 GET，不落审计。已实测核对",
}


# --------------------------------------------------------- 判定：沿调用链找脱敏入口


def _called_names(fn) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _in_router_packages(fn) -> bool:
    return (fn.__module__ or "").startswith(ROUTER_PACKAGES)


def _masks(fn, depth: int = 0, seen: set | None = None, follow=_in_router_packages) -> bool:
    """函数体或其调用的路由包内帮手（最多 MAX_CALL_DEPTH 层）是否出现脱敏入口。"""
    seen = set() if seen is None else seen
    if fn in seen or depth > MAX_CALL_DEPTH:
        return False
    seen.add(fn)
    names = _called_names(fn)
    if names & MASKERS:
        return True
    for name in names:
        target = fn.__globals__.get(name)
        if inspect.isfunction(target) and follow(target) and _masks(target, depth + 1, seen, follow):
            return True
    return False


def _classify() -> tuple[int, dict[str, tuple[str, list[str], str]]]:
    """盘点：(扫描端点数, {key: (path, fields, 判定)})。

    判定：masked / resident / accepted / unmasked（带 PII 字段的契约），
    loose_masked / loose_reviewed / loose_unreviewed（看不出字段的裸契约）。
    """
    rows: dict[str, tuple[str, list[str], str]] = {}
    total = 0
    for route in _iter_routes():
        total += 1
        key = f"{_module_key(route.endpoint.__module__)}:{route.endpoint.__name__}"
        fields = sorted(set(_pii_fields(route.response_model)))
        if fields:
            if _masks(route.endpoint):
                verdict = "masked"
            elif route.path.startswith(RESIDENT_SIDE_PREFIXES):
                verdict = "resident"
            elif key in ACCEPTED_PLAINTEXT_EGRESS:
                verdict = "accepted"
            else:
                verdict = "unmasked"
        elif _is_loose_contract(route.response_model):
            if _masks(route.endpoint):
                verdict = "loose_masked"
            elif key in LOOSE_CONTRACT_REVIEWED:
                verdict = "loose_reviewed"
            else:
                verdict = "loose_unreviewed"
        elif _is_export_route(route):
            if _masks(route.endpoint):
                verdict = "export_masked"
            elif key in EXPORT_NO_PII_REVIEWED:
                verdict = "export_reviewed"
            else:
                verdict = "export_unreviewed"
        else:
            continue
        rows[key] = (route.path, fields, verdict)
    return total, rows


# --------------------------------------------------------- 用例


def test_带PII的出口必须脱敏或书面登记():
    """新增一个返回身份证号/电话却不走 privacy 的端点，这里先红。"""
    total, table = _classify()
    counts = {v: sum(1 for _p, _f, verdict in table.values() if verdict == v)
              for v in ("masked", "resident", "accepted", "unmasked",
                        "loose_masked", "loose_reviewed", "loose_unreviewed")}
    typed = counts["masked"] + counts["resident"] + counts["accepted"] + counts["unmasked"]
    print(
        f"\n[PII 出口守卫] 扫描端点 {total} 个，响应带 id_card/phone 的 {typed} 个："
        f"已脱敏 {counts['masked']}、居民本人侧 {counts['resident']}、"
        f"书面登记明文 {counts['accepted']}、未处置 {counts['unmasked']}；"
        f"裸 dict/Any 契约 {counts['loose_masked'] + counts['loose_reviewed'] + counts['loose_unreviewed']} 个"
        f"（已脱敏 {counts['loose_masked']}、复核登记 {counts['loose_reviewed']}、未复核 {counts['loose_unreviewed']}）"
    )
    leaks = sorted(
        f"{key}（{path}）字段 {fields}"
        for key, (path, fields, verdict) in table.items()
        if verdict == "unmasked"
    )
    assert leaks == [], (
        "以下端点的响应带身份证号/电话，却没有经过 privacy.desensitize / mask_id_card / "
        "mask_phone（CLAUDE.md §8：出口一律经 privacy.py 脱敏）：\n  " + "\n  ".join(leaks)
        + "\n工作人员侧接口非 admin 一律掩码；确属业务必需明文的，登记进 "
        "ACCEPTED_PLAINTEXT_EGRESS 并写明理由（清单只减不增）。"
    )


def test_明文出口登记不得腐烂():
    """登记条目必须仍然成立：端点还在、响应还带 PII、还没脱敏。修掉一条就删一条。"""
    _total, table = _classify()
    stale = sorted(
        f"{key}：{'端点已不存在或不再带 PII' if key not in table else '判定已是 ' + table[key][2]}"
        for key in ACCEPTED_PLAINTEXT_EGRESS
        if table.get(key, ("", [], "gone"))[2] != "accepted"
    )
    assert stale == [], "登记清单里这些条目已不成立，应删除：\n  " + "\n  ".join(stale)


def test_裸契约逐个复核_不许新增未复核的():
    """裸 dict/Any 契约是这道守卫的结构性盲区**之一**：每一个都要么调了脱敏入口、要么人工复核登记。

    （原文写的是「唯一的结构性盲区」。2026-09-10 证伪：非 JSON 导出是第二个，
    见 `test_非JSON导出也必须脱敏或书面复核`。）

    新增一个 `response_model=dict` 的端点会先在这里红——要么改成带字段的模型
    （契约治理也要求这样），要么复核后登记。登记同样不得腐烂。
    """
    _total, table = _classify()
    unreviewed = sorted(f"{key}（{path}）" for key, (path, _f, verdict) in table.items()
                        if verdict == "loose_unreviewed")
    assert unreviewed == [], (
        "以下端点的 response_model 是裸 dict/Any，守卫看不进去，且未复核登记：\n  "
        + "\n  ".join(unreviewed)
    )
    stale = sorted(
        f"{key}：{'端点已不存在或已改成带字段的契约' if key not in table else '判定已是 ' + table[key][2]}"
        for key in LOOSE_CONTRACT_REVIEWED
        if table.get(key, ("", [], "gone"))[2] != "loose_reviewed"
    )
    assert stale == [], "裸契约复核清单里这些条目已不成立，应删除：\n  " + "\n  ".join(stale)


def test_非JSON导出也必须脱敏或书面复核():
    """CSV / NDJSON / HTML / SVG / 文件流：`response_model` 看不出字段，出口照样是出口。

    **这是这道守卫的第二个结构性盲区，2026-09-10 补上。** 此前 `_classify()` 对
    「既没有 PII 字段、又不是裸 dict」的端点直接 `continue`——而 CSV 导出的
    `response_model` 要么是 `str`、要么干脆没有，正好落进那条 `continue`：

        certs.py:export_death_report_cards_csv   列头就写着「身份证号」
        printing.py:print_cert 等 12 个          打印件渲染患者身份证号与电话

    这两处**本来就写对了**（前者走 `_death_card`、后者走 `mask_id_card`/`mask_phone`），
    但那是靠人记得，不是靠闸门——它们停止脱敏时，三条既有用例一条都不会红。

    判据见 `_is_export_route`：`response_class` 与函数源码**两个判据缺一不可**
    （单独任一个只认得 8 个，并起来才 20 个）。
    """
    _total, table = _classify()
    exports = {k: v for k, v in table.items() if v[2].startswith("export_")}
    masked = sum(1 for v in exports.values() if v[2] == "export_masked")
    reviewed = sum(1 for v in exports.values() if v[2] == "export_reviewed")
    print(
        f"\n[PII 出口守卫·非 JSON 导出] 命中 {len(exports)} 个："
        f"已脱敏 {masked}、复核登记无 PII {reviewed}、未处置 "
        f"{len(exports) - masked - reviewed}"
    )
    unreviewed = sorted(
        f"{key}（{path}）" for key, (path, _f, verdict) in exports.items()
        if verdict == "export_unreviewed"
    )
    assert unreviewed == [], (
        "以下端点吐的是非 JSON 响应体（CSV/NDJSON/HTML/SVG/文件流），"
        "既没走 privacy 的脱敏入口，也没经复核登记：\n  " + "\n  ".join(unreviewed)
        + "\n带居民身份证号/电话的，改走 desensitize / mask_id_card / mask_phone；"
        "确实不含个体身份的，复核后登记进 EXPORT_NO_PII_REVIEWED 并写明理由（只减不增）。"
    )
    stale = sorted(
        f"{key}：{'端点已不存在或不再是非 JSON 导出' if key not in table else '判定已是 ' + table[key][2]}"
        for key in EXPORT_NO_PII_REVIEWED
        if table.get(key, ("", [], "gone"))[2] != "export_reviewed"
    )
    assert stale == [], "非 JSON 导出复核清单里这些条目已不成立，应删除：\n  " + "\n  ".join(stale)


def _self_test_helper(value):
    return mask_phone(value)


def _self_test_direct(value):
    return mask_id_card(value)


def _self_test_via_helper(value):
    return _self_test_helper(value)


def _self_test_none(value):
    return value.upper()


def _self_test_deep(value):
    return _self_test_via_helper(value)


def test_守卫自证_沿调用链识别脱敏入口():
    """规则本身：直接调用、经帮手调用、多层帮手都算；没调用的不算；越过层数上限的不算。"""
    follow = lambda fn: fn.__module__ == __name__  # noqa: E731 - 自证时跟本模块的帮手
    assert _masks(_self_test_direct, follow=follow) is True
    assert _masks(_self_test_via_helper, follow=follow) is True
    assert _masks(_self_test_deep, follow=follow) is True
    assert _masks(_self_test_none, follow=follow) is False
    # 层数上限：deep → via_helper → helper 是 2 层，把上限压到 1 就该看不见
    assert _masks(_self_test_deep, depth=MAX_CALL_DEPTH - 1, follow=follow) is False


def test_守卫自证_PII字段递归识别():
    class Inner(BaseModel):
        guardian_id_card: str
        phone_verified: bool  # 前缀形态不算（不是号码本身）

    class Outer(BaseModel):
        name: str
        inner: Inner | None
        items: list[Inner]

    assert _pii_fields(list[Outer]) == ["Inner.guardian_id_card"]
    assert _pii_fields(dict[str, int]) == []
    assert _is_loose_contract(dict) and _is_loose_contract(dict[str, typing.Any])
    assert _is_loose_contract(dict[str, str]) and _is_loose_contract(typing.Any)
    assert not _is_loose_contract(dict[str, int]) and not _is_loose_contract(list[Outer])


# --------------------------------------------------------- 行为回归：本轮盘出来的洞


def test_建档幂等命中既有档案_非admin拿到的是掩码(client, admin):
    """`POST /api/patients` 幂等命中时返回的是别人录入的那份档案：非 admin 必须看到掩码。

    改之前：经办账号只要知道证件号，提交一次"建档"就能拿到该档案的**明文电话**。
    """
    payload = {"name": "套号测试", "id_card": "330281199001019876", "phone": "13900009876"}
    created = client.post("/api/patients", json=payload, headers=admin)
    assert created.status_code == 201, created.text
    assert created.json()["phone"] == "13900009876"  # admin 明文，与既有口径一致

    org = client.post(
        "/api/organizations",
        json={"name": "出口守卫卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    made = client.post(
        "/api/users",
        json={"username": "egress_op", "password": "pass123456", "role": "operator",
              "full_name": "经办", "org_id": org["id"]},
        headers=admin,
    )
    assert made.status_code == 201, made.text
    operator = login(client, "egress_op", "pass123456")

    again = client.post("/api/patients", json={"name": "套号测试", "id_card": payload["id_card"]},
                        headers=operator)
    assert again.status_code == 201, again.text  # 幂等返回既有档案，状态码不变
    body = again.json()
    assert body["ehc_no"] == created.json()["ehc_no"]
    assert body["phone"] == "139******76", body
    assert body["id_card"] == "3302**********9876", body

    fresh = client.post("/api/patients", json={"name": "新建档", "id_card": "330281199001019887",
                                               "phone": "13700001122"}, headers=operator)
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["phone"] == "137******22"  # 新建同样按 H1 口径回显掩码
