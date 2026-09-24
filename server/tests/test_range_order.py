"""区间入参要查起止顺序：起晚于止照收，下游按区间判定的地方就整条失效（区间起止顺序）。

请求模型里成对的区间字段（下限 / 上限、起 / 止）有 9 对，6 对的处理函数里早就查了顺序（冷链温区、劳动合同、
手术排班、派驻、批量放号、慢专病管理目标），3 对没查。2026-09-24 实测（修前代码）：报告推送任务填有效期
起 2026-12-01、止 2026-10-01，201；调度 `今天 < 起` 或 `今天 > 止` 总有一个成立，于是每天都跳过——
任务一直显示「启用中」，一份报告也不生成（冻结在 09-24 / 11-01 / 12-15 各跑一轮，`last_run_at` 始终为空）。
另两对（慢专病服务起止、术中记录起止）眼下只展示，倒置了就是一条自相矛盾的记录。

**判据**（派生、零基线）：请求模型里名字按词元成对（min/max、low/high、start/end、from/to、begin/end、
lower/upper）、类型是日期 / 时刻 / 数值（`*_id` 与字符串不算——`from_org_id` / `to_org_id` 是双方，不是区间）
的两个字段；每个收这个模型的处理函数，函数体里（或它直接调用的本模块函数里）都得有一处把两者放在同一个
比较式里的判断。局部变量顺一层赋值（`low = changes.get("target_low", target.target_low)` 算提到了
target_low）。改档与建档各算各的——只查建档、不查改档，正是改档把区间改倒置的那条路。
"""
from __future__ import annotations

import ast
import re

import pytest
import test_datestr_single_source as ds

PAIR_TOKENS = [("min", "max"), ("low", "high"), ("start", "end"), ("from", "to"), ("begin", "end"),
               ("lower", "upper")]
_RANGE_TYPE = re.compile(r"Date|Time|float|Float|\bint\b")

#: 基线：已清零（2026-09-24 实测 5 个处理函数 → 0：报告推送任务建 / 改、慢专病纳管建 / 改、术中记录）
BASELINE = 0


def range_pairs() -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(定义所在文件, 请求模型名) → 其中的区间字段对。同名模型在不同模块各算各的（schemas 的签约 `ContractCreate`
    与人事的劳动合同 `ContractCreate` 不是一回事）。"""
    models = ds._model_classes()
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for name in ds._request_models(models):
        for path, cls in models[name]:
            typed = {
                s.target.id: ast.unparse(s.annotation) for s in cls.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
            for field, ann in typed.items():
                if field.endswith("_id") or not _RANGE_TYPE.search(ann):
                    continue
                tokens = field.split("_")
                for lo, hi in PAIR_TOKENS:
                    if lo in tokens:
                        other = "_".join(hi if t == lo else t for t in tokens)
                        if other in typed and _RANGE_TYPE.search(typed[other]):
                            key = (path.relative_to(ds.APP_DIR).as_posix(), name)
                            out.setdefault(key, []).append((field, other))
    return out


def _resolve(rel: str, tree: ast.Module, name: str, defined: dict[str, set[str]]) -> str | None:
    """处理函数所在文件里的模型名 → 定义它的文件：本文件定义的优先，其次全仓唯一的，再次按本文件的 `from … import`。"""
    where = defined.get(name, set())
    if rel in where:
        return rel
    if len(where) == 1:
        return next(iter(where))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names) and node.module:
            base = rel.rsplit("/", node.level)[0] if node.level else ""
            target = (f"{base}/" if base else "") + node.module.replace(".", "/") + ".py"
            if target in where:
                return target
    return None


def _mentions(node: ast.AST) -> set[str]:
    """表达式里提到的名字：属性名、变量名、字符串常量（`changes.get("valid_from", …)`）。"""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.add(sub.value)
    return found


def _aliases(fn: ast.AST) -> dict[str, set[str]]:
    """函数内局部变量 → 它的赋值式里提到的名字（顺一层；元组解包逐位对应）。"""
    alias: dict[str, set[str]] = {}
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Assign):
            continue
        for target in sub.targets:
            if isinstance(target, ast.Name):
                alias.setdefault(target.id, set()).update(_mentions(sub.value))
            elif isinstance(target, ast.Tuple) and isinstance(sub.value, ast.Tuple):
                for t, v in zip(target.elts, sub.value.elts):
                    if isinstance(t, ast.Name):
                        alias.setdefault(t.id, set()).update(_mentions(v))
    return alias


def _ordered_in(fn: ast.AST, a: str, b: str) -> bool:
    alias = _aliases(fn)

    def names(expr: ast.AST) -> set[str]:
        got = _mentions(expr)
        return got | {m for n in got for m in alias.get(n, ())}

    for sub in ast.walk(fn):
        if isinstance(sub, ast.Compare):
            operands = [names(x) for x in [sub.left, *sub.comparators]]
            if any(a in x for x in operands) and any(b in y for y in operands) and not any(
                    a in x and b in x for x in operands):
                return True
    return False


def unordered_handlers(sources: list[tuple[str, str]] | None = None, pairs=None) -> list[str]:
    """`文件:处理函数(模型) a/b`：收这个请求模型、却不查这对区间起止顺序的处理函数。"""
    defined: dict[str, set[str]] = {}
    if pairs is None:
        pairs = range_pairs()
        # 定义表要含全部模型（不只带区间的）：签约的 `ContractCreate` 在 schemas 里、没有区间，
        # 只按带区间的定义去认，就会被认成人事那个同名模型
        for name, defs in ds._model_classes().items():
            defined[name] = {path.relative_to(ds.APP_DIR).as_posix() for path, _cls in defs}
    else:
        for where, name in pairs:
            defined.setdefault(name, set()).add(where)
    if sources is None:
        sources = [(p.relative_to(ds.APP_DIR).as_posix(), p.read_text(encoding="utf-8"))
                   for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))]
    bad = []
    for rel, text in sources:
        tree = ast.parse(text)
        funcs = {f.name: f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in funcs.values():
            for arg in fn.args.args:
                name = ast.unparse(arg.annotation) if arg.annotation is not None else ""
                where = _resolve(rel, tree, name, defined) if name in defined else None
                if (where, name) not in pairs:
                    continue
                called = [funcs[c.func.id] for c in ast.walk(fn)
                          if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in funcs]
                for a, b in pairs[(where, name)]:
                    if not any(_ordered_in(f, a, b) for f in [fn, *called]):
                        bad.append(f"{rel}:{fn.name}({name}) {a}/{b}")
    return sorted(bad)


def test_区间入参要查起止顺序_只减不增():
    bad = unordered_handlers()
    assert len(bad) <= BASELINE, (
        f"收区间入参、却不查起止顺序的处理函数 {len(bad)} 处，超过基线 {BASELINE}：\n  " + "\n  ".join(bad)
        + "\n\n起晚于止照收，下游按区间判定的地方就整条失效（报告推送任务有效期倒置即永不运行）。"
        "在处理函数里（改档要与存量合并后再比）加一句顺序判断，422 报人话。"
    )


def test_修完请把基线调小():
    assert len(unordered_handlers()) >= BASELINE, "实测比基线少——把 BASELINE 调小并写上是哪一批"


def test_判据自证():
    pairs = range_pairs()
    # 认得出：日期、时刻、数值的区间；认不出双方 id 与字符串（交接班的交班人 / 接班人）
    assert ("valid_from", "valid_to") in pairs[("spd/routers/followup.py", "ReportTaskPatch")]
    assert ("min_allowed", "max_allowed") in pairs[("routers/vaccine_supply.py", "ColdChainIn")]
    assert ("start_time", "end_time") in pairs[("routers/surgery.py", "ScheduleIn")]
    names = {name for _where, name in pairs}
    assert "ReferralCreate" not in names and "HandoverIn" not in names
    # 同名不同物：慢专病上报任务的 `ReportTaskIn`（care.py）没有有效期，不能拿推送任务那个套上去
    assert ("spd/routers/care.py", "ReportTaskIn") not in pairs
    # 顺一层局部变量：管理目标改档用 `low` / `high` 两个局部变量比，照样算查了
    snippet = '''
def update_target(target_id: int, body: TargetPatch, db=None):
    changes = body.model_dump(exclude_unset=True)
    low = changes.get("target_low", target.target_low)
    high = changes.get("target_high", target.target_high)
    if low is not None and high is not None and low > high:
        raise HTTPException(422)

def create_task(body: ReportTaskIn, db=None):
    _check(body.valid_from, body.valid_to)

def _check(valid_from, valid_to):
    if valid_from and valid_to and valid_from > valid_to:
        raise HTTPException(422)

def update_task(task_id: int, body: ReportTaskPatch, db=None):
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(task, key, value)
'''
    got = unordered_handlers([("自证.py", snippet)], {
        ("自证.py", "TargetPatch"): [("target_low", "target_high")],
        ("自证.py", "ReportTaskIn"): [("valid_from", "valid_to")],
        ("自证.py", "ReportTaskPatch"): [("valid_from", "valid_to")],
    })
    # 建档经本模块函数查了、改档没查：只点名改档
    assert got == ["自证.py:update_task(ReportTaskPatch) valid_from/valid_to"], got


# ---------------------------------------------------------------- 行为回归：修的三处

B = "/api/spd"


@pytest.fixture(scope="module")
def spd_world(client, admin):
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "RANGE-T", "name": "区间顺序", "period": "daily", "sections": [{"key": "summary"}]})
    assert tpl.status_code == 201, tpl.text
    org = client.post("/api/organizations",
                      json={"name": "区间顺序县医院", "org_type": "lead_hospital", "level": "county"},
                      headers=admin).json()
    patient = client.post("/api/patients", json={"name": "区间顺序患者", "id_card": "330281198801019977"},
                          headers=admin).json()
    return {"template": tpl.json()["id"], "org": org["id"], "patient": patient["id"]}


def test_报告推送任务有效期倒置_建与改都422_存量倒置的照样能暂停(client, admin, spd_world, monkeypatch):
    base = {"template_id": spd_world["template"], "name": "有效期", "push_time": "08:00"}
    bad = client.post(f"{B}/report-tasks", headers=admin,
                      json={**base, "valid_from": "2026-12-01", "valid_to": "2026-10-01"})
    assert bad.status_code == 422 and bad.json()["detail"] == "有效期止不得早于有效期起", bad.text
    ok = client.post(f"{B}/report-tasks", headers=admin,
                     json={**base, "valid_from": "2026-10-01", "valid_to": "2026-12-01"})
    assert ok.status_code == 201, ok.text
    tid = ok.json()["id"]
    # 只改一头也能改倒置：与存量合并后再比
    assert client.patch(f"{B}/report-tasks/{tid}", headers=admin, json={"valid_to": "2026-09-01"}).status_code == 422
    assert client.patch(f"{B}/report-tasks/{tid}", headers=admin, json={"valid_from": "2027-01-01"}).status_code == 422
    assert client.patch(f"{B}/report-tasks/{tid}", headers=admin, json={"valid_to": ""}).status_code == 200  # 止留空=长期

    # 换校验之前存进去的倒置任务：暂停 / 删除它不该被拦
    from app.database import SessionLocal
    from app.spd.models import SpdReportTask
    with SessionLocal() as db:
        legacy = SpdReportTask(template_id=spd_world["template"], name="存量倒置", push_time="08:00",
                               valid_from="2026-12-01", valid_to="2026-10-01")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    paused = client.patch(f"{B}/report-tasks/{legacy_id}", headers=admin, json={"status": "paused"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text


def test_纳管服务期倒置_建与改都422(client, admin, spd_world):
    base = {"patient_id": spd_world["patient"], "program_code": "hypertension", "org_id": spd_world["org"]}
    bad = client.post(f"{B}/enrollments", headers=admin,
                      json={**base, "service_start": "2026-10-01", "service_end": "2026-09-01"})
    assert bad.status_code == 422 and bad.json()["detail"] == "服务结束日期不得早于开始日期", bad.text
    ok = client.post(f"{B}/enrollments", headers=admin,
                     json={**base, "service_start": "2026-10-01", "service_end": "2027-09-30"})
    assert ok.status_code == 201, ok.text
    eid = ok.json()["id"]
    assert client.patch(f"{B}/enrollments/{eid}", headers=admin,
                        json={"service_end": "2026-09-30"}).status_code == 422
    assert client.patch(f"{B}/enrollments/{eid}", headers=admin,
                        json={"service_end": "2027-12-31"}).status_code == 200


def test_术中记录起止倒置_422_T写法与空格写法混着也认(client, admin):
    body = {"actual_surgery_name": "阑尾切除术"}
    bad = client.post("/api/surgery/requests/999999/record", headers=admin,
                      json={**body, "start_at": "2026-09-24T10:00", "end_at": "2026-09-24 08:00"})
    assert bad.status_code == 422 and bad.json()["detail"] == "手术结束时间不得早于开始时间", bad.text
    # 顺序对的越过这一道（申请单不存在，落到 404）
    ok = client.post("/api/surgery/requests/999999/record", headers=admin,
                     json={**body, "start_at": "2026-09-24 08:00", "end_at": "2026-09-24T10:00"})
    assert ok.status_code == 404, ok.text
