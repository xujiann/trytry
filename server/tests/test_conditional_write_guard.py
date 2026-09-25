"""写接口的归属判定只在某个 `if` 分支里：别的分支按 id 直写、谁都挡不住（P1-130 的闸门）。

P1-130 是这样漏过横向越权棘轮的：`record_call_result` 里唯一的归属判定写在「接通且挂着随访记录」那一支，其余结果
（未接通 / 取消、挂着复诊或宣教）按任务号直接写；而那几道棘轮只问「函数里出现过守卫没有」——出现过，就算守过了。
P1-73 说的「有条件的守卫」在读侧改用动态探针量，写侧一直没有东西盯。

**闸门**（派生、零基线）：写接口（post / put / patch / delete）函数体里直接调了归属守卫，却**不是每条路都经过**
（单边 `if` 里的不算；`if … else …` 两边都判的算；`for` / `with` / `try` 是逐条或整段都要过的，照算）即点名；同模块
帮手里调了守卫的，帮手被调也算。逐条判过的登记进 `BY_DESIGN`（写明为什么只在分支里判）或 `AWAITING`（登记了待裁定的条目号），两份都只减不增。
"""
import ast
import pathlib

from test_stage15_horizontal import _PATIENT_WRITE_GUARDS, DOMAIN_ORG_GUARDS

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
GUARDS = set(_PATIENT_WRITE_GUARDS) | {name.split(":")[1] for name in DOMAIN_ORG_GUARDS} | {
    "assert_owner_visible", "accessible_patient", "_load_task", "_patient"}

#: 按设计只在分支里判的（逐条写明理由）
BY_DESIGN = {
    "routers/exams.py:amend_report":
        "判的是申请单上的患者；申请单经外键必在，`if req is not None` 只是防御，走不到的分支",
    "routers/inpatient.py:record_order_execution":
        "判的是医嘱所在的住院记录；住院记录经外键必在，`if admission is not None` 只是防御，走不到的分支",
    "spd/routers/config/paths.py:create_path_template":
        "只有挂了机构的路径模板才判机构可写；不挂机构的是全域模板，本就由配置角色（CONFIG_ROLES）维护",
}
#: 已登记待裁定、故意没修的（写明条目号）
AWAITING = {
    "spd/routers/followup.py:record_call_result": "P1-130（并入 P1-71）：回写同时是呼叫中心网关的回调入口，网关身份未建模",
    "spd/routers/population.py:handle_service_apply": "P1-48：服务申请表没有机构列，受理方归属待裁定",
}


def _calls(node) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str:
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")


def _guaranteed(stmts, is_guard) -> bool:
    """这串语句走下来一定经过守卫：分支外有守卫调用；或 `if … else …` 两边都一定经过（互斥二选一，如住院 / 门诊）。
    `for` / `while` / `with` / `try` 视同顺序执行（逐条都判、整段都判）。"""
    for st in stmts:
        if isinstance(st, ast.If):
            if st.orelse and _guaranteed(st.body, is_guard) and _guaranteed(st.orelse, is_guard):
                return True
            continue
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            if _guaranteed(st.body, is_guard):
                return True
            continue
        if any(isinstance(n, ast.Call) and is_guard(n) for n in ast.walk(st)):
            return True
    return False


def conditional_guards(sources: dict[str, str] | None = None) -> list[str]:
    """`文件:函数`——调了归属守卫、却一处都不在分支之外的写接口。"""
    files = {p.relative_to(APP).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(APP.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    found = []
    for rel, text in files.items():
        tree = ast.parse(text)
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        guarding = {name for name, fn in funcs.items() if any(_name(c) in GUARDS for c in _calls(fn))}
        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch", "delete") for d in fn.decorator_list):
                continue
            direct = [c for c in _calls(fn) if _name(c) in GUARDS]
            if not direct:
                continue   # 一个守卫都没有的，归横向越权那几道棘轮管
            def is_guard(call, guarding=guarding):
                return _name(call) in GUARDS or (isinstance(call.func, ast.Name) and call.func.id in guarding)

            if not _guaranteed(fn.body, is_guard):
                found.append(f"{rel}:{fn.name}")
    return sorted(found)


def test_写接口的归属判定不能只在分支里():
    unjudged = sorted(set(conditional_guards()) - set(BY_DESIGN) - set(AWAITING))
    assert not unjudged, (
        "以下写接口的归属守卫只写在 if 分支里，别的分支按 id 直写：\n  " + "\n  ".join(unjudged)
        + "\n\n把判定挪到分支之前（对象一取出来就判）；确属只在某种情形下才需要判的，写明理由登记进 BY_DESIGN。"
    )


def test_登记名单都还在_不留死条目():
    found = set(conditional_guards())
    assert (set(BY_DESIGN) | set(AWAITING)) <= found, sorted((set(BY_DESIGN) | set(AWAITING)) - found)
    assert not set(BY_DESIGN) & set(AWAITING)
    assert all(reason.strip() for reason in [*BY_DESIGN.values(), *AWAITING.values()])


def test_判据自证_分支里的点名_分支外与逐条的放过():
    snippet = (
        "def _load(db, x_id, user):\n    x = db.get(X, x_id)\n    assert_org_writable(db, user, x.org_id)\n    return x\n"
        "@router.post('/a/{x_id}')\ndef only_in_if(x_id, body, db, user):\n    x = db.get(X, x_id)\n"
        "    if body.kind == 'a':\n        assert_org_writable(db, user, x.org_id)\n    x.status = body.status\n"
        "@router.post('/b/{x_id}')\ndef up_front(x_id, body, db, user):\n    x = db.get(X, x_id)\n"
        "    assert_org_writable(db, user, x.org_id)\n    if body.kind == 'a':\n        assert_org_writable(db, user, 1)\n"
        "@router.post('/c')\ndef per_item(body, db, user):\n    for item in body.items:\n"
        "        assert_patient_visible(db, user, item.patient_id)\n"
        "@router.patch('/d/{x_id}')\ndef via_helper(x_id, body, db, user):\n    x = _load(db, x_id, user)\n"
        "    if body.kind:\n        assert_org_writable(db, user, 2)\n"
        "@router.post('/e')\ndef either_or(body, db, user):\n    if body.a:\n        assert_org_writable(db, user, 1)\n"
        "    else:\n        assert_patient_visible(db, user, 2)\n"
        "@router.post('/f')\ndef one_sided_else(body, db, user):\n    if body.a:\n        assert_org_writable(db, user, 1)\n"
        "    else:\n        pass\n"
    )
    assert [f for f in conditional_guards({"probe.py": snippet}) if f.startswith("probe.py")] == [
        "probe.py:one_sided_else", "probe.py:only_in_if"]
