"""删被外键引用着的行：报告推送任务生成过报告还能删（P2-59）。

`DELETE /api/spd/report-tasks/{id}` 直接 `db.delete(task)`，报告实例 `spd_report_instances.task_id` 却外键
指着它（没有 ON DELETE）。2026-09-24 实测（修前代码）：
- **真 PG**：生成过报告的任务一删即 500（`ForeignKeyViolation`，未接住）；
- **开发库（SQLite 不开外键）**：照删 204，实例留着悬空的 `task_id`；SQLite 还会把这个编号复用给下一个新建的
  任务——旧报告从此被认成新任务的，调度按「同任务同期间只出一份」判重时会把它当成新任务已经出过的那份。

修法：生成过报告的任务 409「不能删除；不再推送请改为暂停」（推送任务页本就有暂停）。

**闸门**（派生、零基线）：写接口（DELETE / POST / PATCH）里 `db.delete(x)` 删的行被别的表外键引用着
（ON DELETE 不是 CASCADE / SET NULL），函数里（连同同模块被调函数）就得提到引用方模型（查一眼有没有）
或接住 `IntegrityError`。量出 1 处 → 0。⚠️ 判据宽：「提到引用方」不等于「查过它」，只防一眼都不看。
"""
from __future__ import annotations

import ast

import test_datestr_single_source as ds

from app.database import SessionLocal

B = "/api/spd"


def _task(client, admin, code):
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": code, "name": f"删任务模板{code}", "sections": [{"key": "summary"}]})
    assert tpl.status_code == 201, tpl.text
    task = client.post(f"{B}/report-tasks", headers=admin, json={"template_id": tpl.json()["id"], "name": "推送任务"})
    assert task.status_code == 201, task.text
    return task.json()["id"]


def test_生成过报告的任务不能删_报告仍挂在它上面(client, admin):
    from app.spd.models import SpdReportInstance, SpdReportTask

    task = _task(client, admin, "p259_a")
    inst = client.post(f"{B}/report-instances", json={"task_id": task}, headers=admin)
    assert inst.status_code == 201, inst.text
    r = client.delete(f"{B}/report-tasks/{task}", headers=admin)
    assert r.status_code == 409 and "暂停" in r.json()["detail"], r.text  # 修前：开发库 204，真 PG 500
    with SessionLocal() as db:
        assert db.get(SpdReportTask, task) is not None
        assert db.get(SpdReportInstance, inst.json()["id"]).task_id == task


def test_没生成过报告的任务照删(client, admin):
    task = _task(client, admin, "p259_b")
    assert client.delete(f"{B}/report-tasks/{task}", headers=admin).status_code == 204
    assert client.delete(f"{B}/report-tasks/{task}", headers=admin).status_code == 404


# ================================================================ 闸门：零基线
BASELINE = 0
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _referrers() -> tuple[dict[str, str], dict[str, set[str]]]:
    """(模型 → 表名, 表名 → 以受限外键引用它的模型名)"""
    import app.models  # noqa: F401  先 app.models 再 app.spd.models（P2-51）
    import app.spd.models  # noqa: F401
    from app.database import Base

    table, refs = {}, {}
    for m in Base.registry.mappers:
        table[m.class_.__name__] = m.local_table.name
        for c in m.columns:
            for fk in c.foreign_keys:
                if (fk.ondelete or "").upper() not in ("CASCADE", "SET NULL"):
                    refs.setdefault(fk.column.table.name, set()).add(m.class_.__name__)
    return table, refs


def unguarded_deletes(sources: dict[str, str] | None = None) -> list[str]:
    table, refs = _referrers()
    files = {p.relative_to(ds.APP_DIR).as_posix(): p.read_text(encoding="utf-8")
             for base in ds._ROUTE_DIRS for p in sorted(base.rglob("*.py"))}
    files.update(sources or {})
    found = set()
    for rel, text in files.items():
        tree = ast.parse(text)
        funcs = {f.name: f for f in tree.body if isinstance(f, _FUNCS)}
        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("delete", "post", "patch") for d in fn.decorator_list):
                continue
            scope = [fn] + [funcs[c.func.id] for c in ast.walk(fn) if isinstance(c, ast.Call)
                            and isinstance(c.func, ast.Name) and c.func.id in funcs]
            bound = {t.id: n.value.args[0].id for f in scope for n in ast.walk(f)
                     if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                     and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "get"
                     and len(n.value.args) == 2 and isinstance(n.value.args[0], ast.Name)
                     and n.value.args[0].id in table for t in n.targets if isinstance(t, ast.Name)}
            deleted = {bound[n.args[0].id] for f in scope for n in ast.walk(f)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "delete"
                       and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in bound}
            names = {x.id for f in scope for x in ast.walk(f) if isinstance(x, ast.Name)}
            for model in deleted:
                unchecked = refs.get(table[model], set()) - {model} - names
                if unchecked and "IntegrityError" not in names:
                    found.add(f"{rel}:{fn.name}:{model}")
    return sorted(found)


def test_删被外键引用的行之前得看引用方():
    bad = unguarded_deletes()
    assert len(bad) <= BASELINE, (
        "以下写接口删的行被别的表外键引用着，函数里既不看引用方、也不接 IntegrityError：\n  " + "\n  ".join(bad)
        + "\n\n真 PG 上撞外键即 500；开发库照删，引用方留着悬空的编号（SQLite 还会把编号复用给新行）。"
        "删之前查一眼引用方，有就 409 并告诉用户该怎么办（停用 / 暂停），或者明确地级联处理。"
    )


def test_判据自证_删前没看引用方的当场点名_看了的与接住的不报():
    snippet = '''
@router.delete("/a/{i}")
def bare(i: int, db: Session = Depends(get_db)):
    task = db.get(SpdReportTask, i)
    db.delete(task)

@router.delete("/b/{i}")
def looked(i: int, db: Session = Depends(get_db)):
    task = db.get(SpdReportTask, i)
    if db.query(SpdReportInstance.id).filter(SpdReportInstance.task_id == i).first():
        raise HTTPException(409)
    db.delete(task)

@router.delete("/c/{i}")
def caught(i: int, db: Session = Depends(get_db)):
    task = db.get(SpdReportTask, i)
    try:
        db.delete(task)
        db.commit()
    except IntegrityError:
        raise HTTPException(409)
'''
    got = [k for k in unguarded_deletes({"自证.py": snippet}) if k.startswith("自证.py")]
    assert got == ["自证.py:bare:SpdReportTask"], got
