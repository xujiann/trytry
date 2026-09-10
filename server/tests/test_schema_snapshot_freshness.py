"""`docs/schema/SCHEMA.md` 必须与当下的模型逐字节一致。

## 防的是哪一种失效

CLAUDE.md §4 把这份文件称作**权威列表**，并要求「改了模型重跑
`python scripts/dump_schema.py`」——也就是说，它的现势性**靠人记得**。
本仓库已经反复把「靠人记得」换成棘轮（PII 出口、created_at、契约、越权），
唯独这一份没有：文档漂了，没有任何东西会红。

## 为什么它值得单独一条

漂掉的不只是"文档旧了"。这份快照带的是**列类型 / NOT NULL / index / 外键 /
唯一约束**，而这些恰恰是另外两道闸门**都不看**的东西（2026-09-10 变异审计实测）：

* `test_schema_governance.py::test_核心表结构已冻结` 比的是
  `sorted(table.c.keys())`——**只有列名**。把 `patients.gender` 从
  `String(8)` 改成 `String(16)`，那条用例照样绿（实测）。
* `tests/schema_parity.py::diff_schema` 比的是模型与库的**列名集合**，
  同样看不见类型。

于是「冻结的核心表」上改一个列的类型，全仓没有任何一道闸门会红——
而 §4 明写这类改动**要先写 ADR**。这条用例补的就是这个口子：
类型一变，快照就对不上，改动必须在 diff 里显形。

## 它**不**保证什么（说清楚，别让绿灯值超过它该值的）

它比的是「模型 ↔ 文档」，不是「模型 ↔ 数据库」。
所以「改了模型、也重跑了本脚本、但**没写迁移**」这一种，它照样绿——
那一格归 `test_migration_model_parity.py` 管，而后者目前只比列名集合。
把 parity 扩到类型是另一件事（跨方言的类型 repr 不一致，要先有归一层），
已登记，未做。
"""
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "scripts"))

from dump_schema import OUT, render  # noqa: E402


def test_快照文档与模型逐字节一致():
    """漂了就变红，并直接给出修复动作（重跑生成脚本）。"""
    expected = render()
    actual = OUT.read_text(encoding="utf-8")
    if actual == expected:
        return
    exp_lines, act_lines = expected.splitlines(), actual.splitlines()
    first = next(
        (i for i, (a, b) in enumerate(zip(act_lines, exp_lines)) if a != b),
        min(len(act_lines), len(exp_lines)),
    )
    around = "\n".join(
        f"    第 {n + 1} 行  文档：{act_lines[n] if n < len(act_lines) else '<无>'}\n"
        f"           模型：{exp_lines[n] if n < len(exp_lines) else '<无>'}"
        for n in range(max(0, first - 1), min(first + 2, max(len(act_lines), len(exp_lines))))
    )
    raise AssertionError(
        "docs/schema/SCHEMA.md 与当前模型不一致（改了模型没重跑生成脚本，"
        "或手改了这份「自动生成，勿手改」的文件）。\n"
        f"  文档 {len(act_lines)} 行 / 模型 {len(exp_lines)} 行，首个差异在第 {first + 1} 行：\n"
        f"{around}\n"
        "  修复：cd server && python scripts/dump_schema.py，然后把改动一并提交。\n"
        "  ⚠️ 若改的是 users/organizations/patients/encounters/admissions 五张**冻结核心表**，"
        "按 CLAUDE.md §4 还需先写 ADR。"
    )


def test_这份快照确实带着类型与约束():
    """防空转：快照要是退化成只有表名列名，上面那条就守不住类型漂移了。

    这条钉的不是具体某一行，而是**这份文档的信息量**——它之所以值得当闸门，
    正是因为它带着另外两道闸门都不看的东西（列类型 / NOT NULL / 索引 / 外键 /
    唯一约束）。少了哪一样都要有人来看一眼。
    """
    text = render()
    assert text.count("## ") >= 200, f"表数量骤降到 {text.count('## ')}，生成器多半坏了"
    for token, what in (
        ("VARCHAR(", "列类型"),
        ("NOT NULL", "非空约束"),
        ("_index_ ", "索引"),
        ("_unique_ ", "唯一约束"),
        ("→ ", "外键"),
        ("PK", "主键"),
    ):
        assert token in text, (
            f"快照里没有{what}（找不到 {token!r}）——生成器退化了，"
            "上面那条「逐字节一致」也就守不住这一类漂移了"
        )
    # 冻结核心表的类型必须真的出现在快照里（这正是变异审计发现的那个口子）
    assert "`gender` · VARCHAR(8) · NOT NULL" in text, (
        "patients.gender 的类型没出现在快照里——要么模型改了、要么渲染变了，两种都得看"
    )
