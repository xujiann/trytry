"""阶段十二：信创数据库适配的可移植性护栏。

平台目前在 SQLite 上开发、在 PostgreSQL 上部署，信创场景还要跑达梦/人大金仓。
**没有这些库的环境，就不该假装测过它们**——这里测的不是"能在达梦上跑"，
而是"没有写下明显跑不到达梦上的东西"：SQLite 专有语法、方言特有构造、
金额列还在用浮点。真正的适配验证要在有库的环境按
`docs/信创适配与部署.md` 的清单跑一遍，这一点文档里写清楚了。
"""
import os
import re

import sqlalchemy as sa

from app import models

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
MIGRATION_DIR = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")

# SQLite 专有或强方言依赖的写法。命中即意味着换库会炸。
FORBIDDEN_SQL = [
    (r"\bAUTOINCREMENT\b", "SQLite 专有关键字，用主键自增即可"),
    (r"\bPRAGMA\b", "SQLite 专有指令"),
    (r"\bGROUP_CONCAT\s*\(", "各库函数名不同（PG 是 string_agg），应在 Python 侧拼接"),
    (r"\bstrftime\s*\(", "SQLite 日期函数，应在 Python 侧算或用 SQLAlchemy 表达式"),
    (r"\bdatetime\s*\(\s*'now'", "SQLite 日期函数"),
    (r"\bILIKE\b", "PostgreSQL 专有，应统一用 like + 归一化大小写"),
]


def _python_sources():
    for dirpath, _dirs, files in os.walk(APP_DIR):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                yield path, open(path, encoding="utf-8").read()


# 只扫**手写 SQL**：`text("...")` / `execute("...")` 里的字符串。
# 扫全文件会把 Python 的 `datetime.strftime` 和 `# pragma: no cover` 都误判成
# 库专有写法——那样的护栏很快就会被人加豁免加到失效。
_RAW_SQL = re.compile(
    r"""(?:sa\.text|text|execute|executemany)\s*\(\s*(?P<q>"{3}|'{3}|"|')(?P<sql>.*?)(?P=q)""",
    re.S,
)


def _raw_sql_snippets(source: str):
    for m in _RAW_SQL.finditer(source):
        yield source[: m.start()].count("\n") + 1, m.group("sql")


def test_手写SQL里没有库专有写法():
    offenders = []
    for path, source in _python_sources():
        for line_no, sql in _raw_sql_snippets(source):
            for pattern, why in FORBIDDEN_SQL:
                if re.search(pattern, sql, re.IGNORECASE):
                    offenders.append(
                        f"{os.path.basename(path)}:{line_no} {pattern} —— {why}"
                    )
    assert offenders == [], "以下手写 SQL 换库会炸：\n" + "\n".join(offenders)


def test_手写SQL总量可控():
    """手写 SQL 越多，换库的工作量越大。这条不是禁止，是把量盯住——
    数字涨了要有人解释为什么不能用 ORM 表达。"""
    total = sum(len(list(_raw_sql_snippets(src))) for _p, src in _python_sources())
    assert total <= 15, f"手写 SQL 已达 {total} 处，超出可控范围，请优先用 ORM 表达"


# 金额列的命名族。`debit`/`credit`/`bonus` 是补进来的——阶段十二第一遍只按
# amount/price/cost 这批词批量改类型，把**复式记账的借贷两列**和绩效奖金漏在
# 外面，而 voucher_entries 的借贷正是整个会计模块求和的对象。
MONEY_WORDS = ("amount", "price", "cost", "balance", "total", "fee", "budget",
               "salary", "pay", "debit", "credit", "bonus")

# 名字里带 MONEY_WORDS 但**不是**金额的列。逐个写明理由，不留"反正不是钱"的
# 模糊豁免——豁免一旦可以不写理由，这份清单很快就会变成绕过检查的后门。
NOT_MONEY = {
    "drg_groups.base_weight": "DRG 权重，无量纲",
    "case_summaries.drg_weight": "同上",
    "performance_indicators.weight": "绩效指标权重",
    "performance_formulas.weight": "同上",
    "fund_distributions.weight": "分配份额权重",
    "fund_distributions.share_pct": "分配占比（百分数）",
    "cost_allocation_rules.ratio_pct": "成本分摊比例（百分数）",
    "payroll_records.perf_coefficient": "绩效系数，乘数不是金额",
    "spd_scores.total_score": "慢专病考核综合得分（0-100），不是金额",
}


def test_金额列一律定点数不用浮点():
    """浮点存金额的误差在阶段七露过头，国产库对浮点的处理又与 SQLite 不同。"""
    offenders = []
    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            full = f"{table.name}.{column.name}"
            if full in NOT_MONEY:
                continue
            if not any(w in column.name for w in MONEY_WORDS):
                continue
            if isinstance(column.type, sa.Float):
                offenders.append(full)
    assert offenders == [], f"这些金额列仍是 Float，应改为 Money（Numeric）：{offenders}"


def test_豁免清单里的列确实存在():
    """防止豁免清单随表结构变化而腐烂——列改名或删掉之后，
    豁免条目会一直挂着，下次真有同名金额列时就被静默放行了。"""
    all_columns = {
        f"{t.name}.{c.name}"
        for t in models.Base.metadata.tables.values()
        for c in t.columns
    }
    stale = sorted(set(NOT_MONEY) - all_columns)
    assert stale == [], f"豁免清单里这些列已不存在，应删除：{stale}"


def test_金额列精度足够县域量级():
    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            if type(column.type) is sa.Numeric:
                assert column.type.precision == 14, f"{table.name}.{column.name}"
                assert column.type.scale == 2, f"{table.name}.{column.name}"
                # asdecimal=False 是明确的取舍，见 models.Money 的说明
                assert column.type.asdecimal is False, f"{table.name}.{column.name}"


def test_部分唯一索引带方言参数():
    """D-2 的部分唯一索引：SQLite 与 PostgreSQL 语法不同，两边都要给。

    国产库若不支持部分索引，适配时改为"全域池写哨兵值 0 而非 NULL"——
    这条替代方案写在模型注释里，不是临场想的。
    """
    index = next(
        i for i in models.FundPool.__table__.indexes if i.name == "uq_fund_pool_global"
    )
    assert index.unique is True
    kwargs = index.dialect_options
    assert kwargs["sqlite"]["where"] is not None
    assert kwargs["postgresql"]["where"] is not None


def test_迁移脚本不写死SQLite():
    """迁移里出现 SQLite 专有 DDL，换库时第一步就过不去。"""
    offenders = []
    for name in os.listdir(MIGRATION_DIR):
        if not name.endswith(".py"):
            continue
        source = open(os.path.join(MIGRATION_DIR, name), encoding="utf-8").read()
        for line_no, sql in _raw_sql_snippets(source):
            for pattern, why in FORBIDDEN_SQL:
                if re.search(pattern, sql, re.IGNORECASE):
                    offenders.append(f"{name}:{line_no} {pattern} —— {why}")
    assert offenders == [], "迁移脚本里有库专有写法：\n" + "\n".join(offenders)


def test_字符串列长度都有上限():
    """无长度 String 在 PostgreSQL 上是 TEXT，在达梦上可能直接建不出来。"""
    offenders = [
        f"{t.name}.{c.name}"
        for t in models.Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, sa.String) and not isinstance(c.type, sa.Text)
        and c.type.length is None
    ]
    assert offenders == [], f"这些 String 列没有长度上限：{offenders}"
