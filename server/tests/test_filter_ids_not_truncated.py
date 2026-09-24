"""闸门：筛选条件里的 id 名单不许先截断再用（P1-83 防复发，零基线）。

P1-83 的形状：

    ids = [p.id for p in db.query(Patient).filter(Patient.name.contains(keyword)).limit(500)]
    query = query.filter(SpdEnrollment.patient_id.in_(ids or [0]))

截断藏在**筛选条件**里：匹配的患者超过上限时只在任意 N 个里找，其余人的记录静默不出现，
总数头也跟着少。列表分页棘轮（`test_list_pagination_ratchet.py`）看的是返回列表那句的 `.limit`，
看不到这里；四处都是逐个实测才撞出来的。改法是子查询（`IN (SELECT id FROM ... WHERE ...)`），
不需要先把 id 取到 Python 里。

判据：路由函数里，`名字 = [... for ... in <带 .limit( 的表达式>]` 或 `名字 = <带 .limit( 的查询>.all()`
取出来的名单，后面又出现在 `.in_(名字` 里。零基线、零豁免——真要「只取前 N 个」，
那是业务规则，写成子查询里的 `.limit()` 也同样被这条认出来，需要先想清楚再来改判据。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import astcode

#: 零基线：一条都不许有（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0

ROOTS = [pathlib.Path(__file__).resolve().parent.parent / "app" / "routers",
         pathlib.Path(__file__).resolve().parent.parent / "app" / "spd" / "routers"]


def _truncated_names(fn: ast.AST) -> dict[str, int]:
    """函数里「由带 `.limit(` 的查询取出来的名单」：名字 → 行号。"""
    out: dict[str, int] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        source = None
        if isinstance(value, (ast.ListComp, ast.SetComp)):
            source = ast.unparse(value.generators[0].iter)
        elif isinstance(value, ast.Call) and ast.unparse(value.func).endswith(".all"):
            source = ast.unparse(value.func)
        if source and ".limit(" in source:
            out[node.targets[0].id] = node.lineno
    return out


def _violations(sources: dict[str, str] | None = None) -> list[str]:
    found = []
    files = {}
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            files[str(path.relative_to(root.parent.parent))] = path.read_text(encoding="utf-8")
    files.update(sources or {})
    for name, text in files.items():
        tree = ast.parse(text)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            names = _truncated_names(fn)
            if not names:
                continue
            code = astcode.code(fn)
            for var, line in names.items():
                if f".in_({var}" in code:
                    found.append(f"{name}:{fn.name}（第 {line} 行的 {var}）")
    return sorted(found)


def test_筛选用的id名单不许先截断():
    bad = _violations()
    assert len(bad) <= BASELINE, (
        "以下函数先用带 .limit() 的查询取出一批 id，再拿去 .in_() 筛——匹配的超过上限时静默少一截（P1-83）：\n  "
        + "\n  ".join(bad)
        + "\n\n改成子查询：`.in_(select(Model.id).where(...))`，匹配条件照搬即可。"
    )


@pytest.mark.parametrize("snippet", [
    # P1-83 修复前的原样
    "def f(db, query, keyword):\n"
    "    ids = [p.id for p in db.query(Patient).filter(Patient.name.contains(keyword)).limit(500)]\n"
    "    return query.filter(SpdCandidate.patient_id.in_(ids or [0]))\n",
    # 同一个病换成 .all() 的写法
    "def f(db, query, keyword):\n"
    "    rows = db.query(Patient).filter(Patient.name.contains(keyword)).limit(200).all()\n"
    "    return query.filter(SpdCaseReport.patient_id.in_(rows))\n",
])
def test_判据自证_修复前的形状当场点名(snippet):
    assert _violations({"自证.py": snippet}) == ["自证.py:f（第 2 行的 {}）".format("ids" if "ids =" in snippet else "rows")]


def test_判据自证_子查询与页内回填不算():
    fine = (
        "def f(db, query, keyword, rows):\n"
        "    query = query.filter(SpdCandidate.patient_id.in_(select(Patient.id).where(Patient.name.contains(keyword))))\n"
        "    names = {p.id: p.name for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows]))}\n"
        "    return query, names\n"
    )
    assert _violations({"自证.py": fine}) == []
