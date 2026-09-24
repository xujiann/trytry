"""天数 / 分钟数入参没有上界就进 `timedelta`：传个大数，`date ± timedelta` 溢出，整个请求 500（P1-96）。

2026-09-24 实测（修前代码，开发库）：

- 到期合同 `days`、未确认危急值 `timeout_minutes`、传染病多点预警 `window_days`、知识库到期 `days`、
  中药制剂批次到期 `days` 五个查询参数传 99999999（到期合同传 -99999999 同样）全部 500
  （`OverflowError: date value out of range`）；
- 慢病病种随访周期只限到列容量 `INT4_MAX`，写成 99999999 照存，此后该病种**建档**、**随访**算「下次到期日」
  时 500——存进库的配置把下游流程一起拖垮，与 P1-94 的慢专病随访方案时间点同一个形状。
- 审计统计 `days` 在函数里先钳到 1~365，本就不炸（判据认得这种钳法）。

修法：五个查询参数补 `Query(le=3650)`（危急值超时按分钟折算同样 10 年）、0 照旧合法；中药批次原先对负数的
`max(days, 0)` 保留。病种随访周期改为业务上限 1~3650，与慢专病管理目标的随访周期同一口径。

判据（AST + 请求模型，平台与慢专病两边的路由）：端点函数里 `timedelta(days|hours|minutes|weeks|seconds=…)`
的实参用到了端点的 int 参数，该参数的 `Query(...)` 里得有 `le` / `lt`，或在用之前经 `min(...)` 钳过；
用到请求体字段（`body.x`）的，请求模型里该字段得有上界。管不到的：非端点的 helper 收天数（调用方各自负责，
今天两处调用方都有界）、存在库里的配置（病种随访周期这类）——那要靠写入口的业务上限与回归用例守着。
"""
import ast
import importlib
import pathlib
import types

import pytest
from fastapi import Query
from pydantic import BaseModel, Field

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 5 处（见 docstring），同一批补完。
BASELINE = 0

_UNITS = ("days", "hours", "minutes", "weeks", "seconds")
_VERBS = ("get", "post", "put", "patch", "delete")


def _router_sources():
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "__init__.py":
                continue
            rel = p.relative_to(APP_DIR)
            module = importlib.import_module("app." + ".".join(rel.with_suffix("").parts))
            yield str(rel), p.read_text(encoding="utf-8"), vars(module)


def _is_endpoint(fn) -> bool:
    return any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _VERBS
               for d in fn.decorator_list)


def _calls_min(node) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "min"
               for n in ast.walk(node))


def _bounded_query(default) -> bool:
    return (isinstance(default, ast.Call) and ast.unparse(default.func).endswith("Query")
            and any(k.arg in ("le", "lt") for k in default.keywords))


def _clamped(fn, name: str) -> bool:
    """用之前先钳过：`days = max(1, min(days, 365))` 这一类。"""
    return any(isinstance(node, ast.Assign) and _calls_min(node.value)
               and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
               for node in ast.walk(fn))


def _field_bounded(model, field: str) -> bool:
    info = getattr(model, "model_fields", {}).get(field)
    return info is not None and any(
        getattr(m, "le", None) is not None or getattr(m, "lt", None) is not None for m in info.metadata)


def unbounded_duration_params(sources=None) -> list[str]:
    """`文件:端点:参数`——端点的 int 参数 / 请求体字段没有上界就进了 `timedelta`。"""
    out = set()
    for rel, text, namespace in (sources if sources is not None else _router_sources()):
        for fn in ast.parse(text).body:
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_endpoint(fn)):
                continue
            positional = fn.args.args
            defaults = dict(zip([a.arg for a in positional[len(positional) - len(fn.args.defaults):]],
                                fn.args.defaults))
            defaults.update({a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None})
            annotations = {a.arg: ast.unparse(a.annotation) for a in positional + fn.args.kwonlyargs
                           if a.annotation is not None}
            ints = {name for name, ann in annotations.items() if ann in ("int", "int | None")}
            bodies = {name: namespace.get(ann) for name, ann in annotations.items()
                      if isinstance(namespace.get(ann), type) and issubclass(namespace[ann], BaseModel)}
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("timedelta")):
                    continue
                for kw in call.keywords:
                    if kw.arg not in _UNITS or _calls_min(kw.value):
                        continue    # 实参里当场钳过的不看
                    for n in ast.walk(kw.value):
                        if isinstance(n, ast.Name) and n.id in ints \
                                and not _bounded_query(defaults.get(n.id)) and not _clamped(fn, n.id):
                            out.add(f"{rel}:{fn.name}:{n.id}")
                        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                                and n.value.id in bodies and not _field_bounded(bodies[n.value.id], n.attr):
                            out.add(f"{rel}:{fn.name}:{n.value.id}.{n.attr}")
    return sorted(out)


# ================================================================ 闸门
def test_天数入参进timedelta之前得有上界():
    offenders = unbounded_duration_params()
    assert len(offenders) <= BASELINE, (
        "端点的天数 / 分钟数入参没有上界就进了 timedelta——传个大数即 OverflowError、500。"
        f"补 `Query(le=…)`（请求体字段补 `Field(le=…)`），或用之前先 `min(...)` 钳住：{offenders}"
    )


def test_判据自证_无界的点名_有界与钳过的不报():
    snippet = '''
class WinIn(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    loose_days: int = 7

@router.get("/a")
def loose(days: int = 30):
    return today + timedelta(days=days)

@router.get("/b")
def bounded(days: int = Query(default=30, ge=0, le=3650)):
    return today + timedelta(days=days)

@router.get("/c")
def clamped(days: int = 30):
    days = max(1, min(days, 365))
    return now - timedelta(days=days)

@router.get("/d")
def inline(hours: int = 48):
    return now - timedelta(hours=max(min(hours, 720), 1))

@router.post("/e")
def body_ok(body: WinIn):
    return today + timedelta(days=body.days)

@router.post("/f")
def body_loose(body: WinIn):
    return today + timedelta(days=body.loose_days)

@router.get("/g")
def minutes_loose(timeout_minutes: int = 30):
    return now - timedelta(minutes=timeout_minutes * 2)

def helper(days: int):
    return today + timedelta(days=days)
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "Query": Query, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    assert unbounded_duration_params([("自证.py", snippet, vars(mod))]) == [
        "自证.py:body_loose:body.loose_days",
        "自证.py:loose:days",
        "自证.py:minutes_loose:timeout_minutes",
    ]


# ================================================================ 行为回归（修前实测见 docstring）
@pytest.mark.parametrize("url", [
    "/api/mgmt/staff-contracts/expiring?days=99999999",
    "/api/mgmt/staff-contracts/expiring?days=-99999999",
    "/api/exams/critical/unacknowledged?timeout_minutes=9999999999999",
    "/api/infectious/alerts?window_days=99999999",
    "/api/knowledge/expiring?days=99999999",
    "/api/tcm/preparation-batches/expiring?days=99999999",
])
def test_天数越界是422而不是500(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 422, (url, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("url", [
    "/api/mgmt/staff-contracts/expiring?days=3650",
    "/api/mgmt/staff-contracts/expiring?days=0",
    f"/api/exams/critical/unacknowledged?timeout_minutes={3650 * 24 * 60}",
    "/api/infectious/alerts?window_days=3650",
    "/api/knowledge/expiring?days=3650",
    "/api/tcm/preparation-batches/expiring?days=3650",
    "/api/tcm/preparation-batches/expiring?days=-5",   # 负数照旧按 0 算
])
def test_上界以内照常(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 200, (url, resp.status_code, resp.text[:200])


def test_病种随访周期越过业务上限是422_上限以内照常建档(client, admin):
    bad = client.post("/api/chronic/disease-types", headers=admin,
                      json={"code": "p196_bad", "name": "周期越界病", "followup_interval_days": 99999999})
    assert bad.status_code == 422, bad.text
    ok = client.post("/api/chronic/disease-types", headers=admin,
                     json={"code": "p196_ok", "name": "周期十年病", "followup_interval_days": 3650})
    assert ok.status_code == 201, ok.text
    patched = client.patch(f"/api/chronic/disease-types/{ok.json()['id']}", headers=admin,
                           json={"followup_interval_days": 99999999})
    assert patched.status_code == 422, patched.text
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "周期回归院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "周期回归", "id_card": "110101199001019696", "gender": "女",
        "birth_date": "1990-01-01", "phone": "13800009696"}).json()["id"]
    enrolled = client.post("/api/chronic", headers=admin,
                           json={"patient_id": patient, "disease": "p196_ok", "managed_by_org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    assert enrolled.json()["next_due"] > "2030-01-01"   # 十年周期照常算出到期日，不溢出
