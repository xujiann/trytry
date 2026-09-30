"""DRG 出院入组时两组匹配分并列，取的是库先返回的那一组（P2-963，第二十七批「排名、Top-N、并列与空值」扫描 G4-1 的
「结果要确定」那一半）。

`assign_drg_group` 遍历 `db.query(DrgGroup)…all()`，不带 ORDER BY，只有严格更高分才换组——并列时留下先返回的那组；事前提示
（`drg_pre_check`）同一个查询、按分数排后截前 5。几种常见合并诊断正好同分（「肺部感染，呼吸衰竭」ES31 与 EJ15 都是 14，
权重 0.95 对 1.90）。PG 不保证无 ORDER BY 的次序：对一组「调权」（原地 UPDATE）这一行就挪到堆尾，同一句诊断改入另一组
（P2-304 在真 PG 上实测过同形问题）。修法与转诊规则试算（P2-304）、随访方案匹配（P2-369）同一个次序：按组编号。并列时入哪组
的规矩另行待裁定。
"""
import ast
import inspect
import textwrap

from app.routers import drgs


def _group_queries(func):
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all"
            and "query(DrgGroup)" in ast.unparse(node)]


def test_出院入组按组编号遍历():
    chains = _group_queries(drgs.assign_drg_group)
    assert chains and all("order_by(DrgGroup.id)" in c for c in chains), chains   # 修前不排序


def test_事前提示与出院入组同一个次序():
    chains = _group_queries(drgs.drg_pre_check)
    assert chains and all("order_by(DrgGroup.id)" in c for c in chains), chains
