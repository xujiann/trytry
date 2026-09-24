"""调阅留痕的"看了哪份""凭什么"必须是人话（P2-42）。

`GET /api/access-logs/mine` 是《个保法》第 44 条落到居民手里的那一页："谁、凭什么、
看了我的哪份档案"。`resource_name` / `basis_name` 取自 `access_logs` 里的两张词表，
查不到就原样回英文码——而词表曾只有 14 个词，源码里实际写进 AccessLog 的有 80 多个：
居民读到的是"看了哪份：inpatient_order""依据：delegate"，监管页也一样。

词表缺词不会报任何错，所以这里**从源码推导全部写入点**，逐个核对都有名字：

- 汇点：`visibility` 里六个带 `resource` 形参的留痕函数 + `AccessLog(...)` 直接构造；
  参数下标从函数签名现读，不写死；
- 包装：自己带 `resource` 形参、又把它原样转交给汇点（或别的包装）的函数——
  `_admission_visible_or_404`、居民端的 `accessible_patient`、spd 居民端的 `_patient`……
  求不动点得出，新写一个包装不用登记；
- 在汇点/包装的每个调用处取 resource 实参：字面量 → 收进来；`None` → 写接口不留读痕，
  跳过；外层函数自己的 `resource` 形参 → 透传，跳过；`_resource(...)` → 附件的
  `att:{owner}:{action}`，按业务域另核；缺省 → 取签名默认值；**其余一律红**（推不出的
  写法不许悄悄漏过）。

`basis` 同理：关键字/位置实参里的字面量（含 `"self" if … else "delegate"` 的两支）+
可见性判定 `patient_basis` / `_patient_basis_uncached` 的返回字面量。
"""
import ast
import pathlib

from app.routers import access_logs, attachments

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 真正落 AccessLog 的函数（visibility 里）与 ORM 构造。
SINK_FUNCS = (
    "assert_patient_visible",
    "scope_patient_list",
    "log_patient_access",
    "log_resident_access",
    "_write_access_log",
    "_write_access_row",
)
BASIS_FUNCS = ("patient_basis", "_patient_basis_uncached")


def _trees():
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(APP.rglob("*.py"))}


def _callee(call: ast.Call) -> str | None:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _params(fn) -> list[str]:
    return [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]


def _param_spec(fn, name: str):
    """(位置下标或 None, 默认值节点或 None)。"""
    pos = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if name in pos:
        i = pos.index(name)
        defaults = fn.args.defaults
        j = i - (len(pos) - len(defaults))
        return i, defaults[j] if j >= 0 else None
    kwo = [a.arg for a in fn.args.kwonlyargs]
    i = kwo.index(name)
    return None, fn.args.kw_defaults[i]


def _arg(call: ast.Call, name: str, index: int | None):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    if index is not None and len(call.args) > index:
        return call.args[index]
    return None


def _enclosing_functions(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node):
        up = parents.get(node)
        while up is not None and not isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
            up = parents.get(up)
        return up

    return enclosing


def _sinks(trees, param: str):
    """汇点 + 包装：函数名 → (位置下标, 默认值节点)。"""
    defs = {}
    for tree in trees.values():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and param in _params(fn):
                defs.setdefault(fn.name, []).append(fn)
    spec = {}
    for name in SINK_FUNCS:
        if name not in defs:
            continue  # 该汇点不收这个形参（如 assert_patient_visible 的 basis 是现算的）
        (fn,) = defs[name]  # 汇点在 visibility 里各只有一份定义
        spec[name] = _param_spec(fn, param)
    spec["AccessLog"] = (None, None)
    changed = True
    while changed:
        changed = False
        for name, fns in defs.items():
            if name in spec:
                continue
            for fn in fns:
                forwards = any(
                    isinstance(node, ast.Call) and _callee(node) in spec
                    and isinstance(a := _arg(node, param, spec[_callee(node)][0]), ast.Name)
                    and a.id == param
                    for node in ast.walk(fn)
                )
                if forwards:
                    spec[name] = _param_spec(fn, param)
                    changed = True
                    break
    return spec


def _assigned_from(fn, name: str) -> set[str]:
    """函数体里给局部变量 name 赋值的调用名集合（`x = f(...)` 的 f）；有非调用赋值则含 ""。"""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            out.add(_callee(node.value) or "" if isinstance(node.value, ast.Call) else "")
    return out


def _literals(node) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _literals(node.body), _literals(node.orelse)
        if body is not None and orelse is not None:
            return body + orelse
    return None


def _collect(param: str):
    """(字面量 → 出处, 附件动态串出处, 推不出的写法)。"""
    trees = _trees()
    spec = _sinks(trees, param)
    found: dict[str, list[str]] = {}
    dynamic, unknown = [], []
    for path, tree in trees.items():
        enclosing = _enclosing_functions(tree)
        rel = path.relative_to(APP.parent)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee(node) in spec):
                continue
            index, default = spec[_callee(node)]
            value = _arg(node, param, index)
            where = f"{rel}:{node.lineno}"
            if value is None:
                value = default
                if value is None:
                    unknown.append(f"{where} 没传 {param} 且签名无默认值")
                    continue
            if isinstance(value, ast.Constant) and value.value is None:
                continue  # 写接口：不落读痕
            literals = _literals(value)
            if literals is not None:
                for lit in literals:
                    found.setdefault(lit, []).append(where)
                continue
            outer = enclosing(node)
            if isinstance(value, ast.Name) and value.id == param and outer is not None \
                    and param in _params(outer):
                continue  # 包装函数把自己的形参转交下去
            if isinstance(value, ast.Call) and _callee(value) == "_resource":
                dynamic.append(where)
                continue
            if isinstance(value, ast.Name) and outer is not None \
                    and _assigned_from(outer, value.id) and _assigned_from(outer, value.id) <= set(BASIS_FUNCS):
                continue  # basis = patient_basis(...)：取值由 _basis_returns() 另收
            unknown.append(f"{where} {ast.unparse(value)}")
    return spec, found, dynamic, unknown


def _basis_returns() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, tree in _trees().items():
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in BASIS_FUNCS:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Return) and node.value is not None:
                        for lit in _literals(node.value) or []:
                            out.setdefault(lit, []).append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    return out


def test_每个写进留痕的resource都有可读名():
    _, found, _, unknown = _collect("resource")
    assert not unknown, (
        "这些留痕调用的 resource 实参守卫推不出来——请写成字面量，"
        "或教会本守卫认这种写法（别让新词绕过词表核对）：\n" + "\n".join(unknown)
    )
    missing = {code: where for code, where in found.items()
               if access_logs.resource_name(code) == code}
    assert not missing, (
        "这些 resource 写进了 AccessLog，却没有可读名——居民在"
        "「谁看过我的档案」里会看到英文码。请补进 access_logs.RESOURCE_NAMES：\n"
        + "\n".join(f"  {c}: {w[:3]}" for c, w in sorted(missing.items()))
    )


def test_附件留痕的每个业务域和动作都有可读名():
    """附件的 resource 是 `att:{owner_type}:{action}` 拼出来的，只有患者档会落留痕。

    子系统的业务域是**装载时**注册的（`spd.register_spd` → `register_attachment_owner`），
    只 import 路由模块看不到——先装载应用，否则本用例的结论取决于测试顺序。
    """
    import app.main  # noqa: F401 - 装载即注册子系统的附件业务域

    _, _, dynamic, _ = _collect("resource")
    assert dynamic, "判据失灵：附件的 _resource(...) 调用一处都没扫到"
    patient_owners = [o for o, s in attachments._OWNERS.items() if s.scope == "patient"]
    assert patient_owners, "判据失灵：附件没有一个患者档业务域"
    from app.spd import spd_enabled

    if spd_enabled():
        assert "spd_task" in patient_owners, "判据失灵：子系统已启用，却没看到它注册的附件业务域"
    missing = [
        attachments._resource(owner, action)
        for owner in patient_owners
        for action in attachments.ACTIONS
        if access_logs.resource_name(attachments._resource(owner, action))
        == attachments._resource(owner, action)
    ]
    assert not missing, (
        "这些附件留痕没有可读名，请补 access_logs.ATTACHMENT_OWNER_NAMES / "
        f"ATTACHMENT_ACTION_NAMES：{missing}"
    )


def test_每个写进留痕的basis都有可读名():
    _, found, _, unknown = _collect("basis")
    assert not unknown, "这些留痕调用的 basis 实参守卫推不出来：\n" + "\n".join(unknown)
    for code, where in _basis_returns().items():
        found.setdefault(code, []).extend(where)
    missing = {c: w for c, w in found.items() if c not in access_logs.BASIS_NAMES}
    assert not missing, (
        "这些 basis 写进了 AccessLog，却没有可读名，请补进 access_logs.BASIS_NAMES：\n"
        + "\n".join(f"  {c}: {w[:3]}" for c, w in sorted(missing.items()))
    )


def test_判据自证():
    """每一种推导路径各举一个已知的点：哪条路径失灵，这里先红，而不是上面静默全绿。"""
    spec, found, _, _ = _collect("resource")
    # 包装是求出来的，不是登记的
    for wrapper in ("_admission_visible_or_404", "_encounter_visible", "_report_visible_or_404",
                    "accessible_patient", "_patient", "assert_owner_visible"):
        assert wrapper in spec, f"判据失灵：没认出包装函数 {wrapper}"
    assert "case_summary" in found          # 经 _admission_visible_or_404 包装、关键字传入
    assert "spd_journey" in found           # spd 居民端 _patient → accessible_patient → log_resident_access
    assert "death_report_card" in found     # log_patient_access 位置实参
    assert "consent" in found               # scope_patient_list 第 6 个位置实参
    assert "access_log_view" in found       # AccessLog(...) 直接构造
    assert "archive" in found               # 缺省实参取签名默认值（revise_report 没传 resource）
    assert len(found) >= 80, len(found)
    _, basis, _, _ = _collect("basis")
    assert {"self", "delegate"} <= set(basis)   # `"self" if … else "delegate"` 两支都收
    assert {"encounter", "contract", "authorization"} <= set(_basis_returns())


def test_可读名的取法():
    assert access_logs.resource_name("inpatient_order") == "住院医嘱"
    assert access_logs.resource_name("att:exam_report:download") == "检查报告附件下载"
    # 认不出的原样返回：旧行里的历史词、以后新增的词都不会因此 500
    assert access_logs.resource_name("att:unknown:download") == "att:unknown:download"
    assert access_logs.resource_name("att:exam_report") == "att:exam_report"
    assert access_logs.resource_name("no_such_code") == "no_such_code"


def test_居民在调阅记录里读到的是人话(client):
    """端到端：家庭代管人读被代管成员的住院押金，成员本人的调阅记录里看到的是中文。"""
    from app.database import SessionLocal
    from app.models import AccessLog

    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    pid = client.post("/api/patients", json={"name": "留痕可读名患者", "id_card": "320000199606066672"},
                      headers=headers).json()["id"]
    db = SessionLocal()
    try:
        for resource in ("deposit", "att:referral:list"):
            db.add(AccessLog(user_id=None, username="resident:1", org_id=None, patient_id=pid,
                             resource=resource, basis="delegate"))
        db.commit()
    finally:
        db.close()
    r = client.get(f"/api/access-logs?patient_id={pid}", headers=headers)
    assert r.status_code == 200, r.text
    names = {(row["resource"], row["resource_name"], row["basis_name"]) for row in r.json()}
    assert ("deposit", "住院押金", "家庭代管") in names
    assert ("att:referral:list", "转诊附件清单", "家庭代管") in names
