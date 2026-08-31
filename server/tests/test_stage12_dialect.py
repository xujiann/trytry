"""阶段十二：信创数据库适配的可移植性护栏。

平台目前在 SQLite 上开发、在 PostgreSQL 上部署，信创场景还要跑达梦/人大金仓。
**没有这些库的环境，就不该假装测过它们**——这里测的不是"能在达梦上跑"，
而是"没有写下明显跑不到达梦上的东西"：SQLite 专有语法、方言特有构造、
金额列还在用浮点。真正的适配验证要在有库的环境按
`docs/信创适配与部署.md` 的清单跑一遍，这一点文档里写清楚了。
"""
import os
import re
import warnings

import sqlalchemy as sa

from app import models
from app.main import app as _app  # noqa: F401  触发全部模型 import（含 spd 子系统）

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
    数字涨了要有人解释为什么不能用 ORM 表达。

    15 → 17（2026-08-31，P1-9 启动种子化加固）：新增 `main.py` 的
    `SELECT pg_advisory_lock/unlock(:key)` 两处——PG 咨询锁没有 ORM 表达
    （与审计链既有的 `pg_advisory_xact_lock` 同族），且按方言分流只在 PG
    上执行，换库不受影响。这两处是本仓库"手写 SQL 的合法理由"之一：
    锁定原语本就是方言特性，硬套 ORM 只会造出更难审的抽象。"""
    total = sum(len(list(_raw_sql_snippets(src))) for _p, src in _python_sources())
    assert total <= 17, f"手写 SQL 已达 {total} 处，超出可控范围，请优先用 ORM 表达"


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


# ---------------------------------------------------------------------------
# 把"记住给金额列起对名字"倒过来：任何 Float 列都必须被登记
# ---------------------------------------------------------------------------
# 上面的 MONEY_WORDS 是一份**坏清单**：它要求写新代码的人记得让金额列的名字
# 落在这十二个词里，否则守卫看都不看它。忘记的后果是**静默**的——新写的
# `subsidy` / `copay` / `deductible` 列用 Float 落库，测试照绿，等到 PG 上
# SUM 累出误差才发现。这份清单自己就栽过一次：第一遍批量改类型时，
# `debit`/`credit`/`bonus` 三个词不在词表里，复式记账的借贷两列被整批漏掉，
# 而 voucher_entries 的借贷正是会计模块求和的对象（见上面的注释）。
#
# 倒过来之后，分母不再是"想到的词"，而是**模型元数据里全部 Float 列**：
# 每一个 Float 列都必须在下面登记并写明"它为什么不是钱"，登记不了就得改成
# Money。新增一个 Float 列而不登记 → 当场变红，不再取决于它叫什么名字。
#
# 这是"好清单"的形态（CLAUDE.md 与第 14 章的口径）：只增不改地记录**已知的
# 例外**，漏更新的后果是测试红而不是静默错；且每条都要答得出理由。
FLOAT_NOT_MONEY: dict[str, str] = {
    # —— 体征/检验测量值：连续量，精度要求与金额无关 ——
    "child_visits.height_cm": "儿童身高，厘米",
    "child_visits.weight_kg": "儿童体重，公斤",
    "vital_sign_records.temperature": "体温",
    "vital_sign_records.weight_kg": "体重，公斤",
    "emergency_vitals.heart_rate": "心率",
    "emergency_vitals.sbp": "收缩压",
    "emergency_vitals.dbp": "舒张压",
    "emergency_vitals.spo2": "血氧饱和度",
    "followups.sbp": "随访收缩压",
    "followups.dbp": "随访舒张压",
    "followups.glucose": "随访血糖",
    "health_monitor_records.value": "居民自测监测值（血压/血糖/体重等混用一列）",
    "health_monitor_records.threshold": "监测告警阈值，与 value 同量纲",
    "qc_measurements.value": "室内质控测定值",
    "qc_lots.target_value": "质控品靶值",
    "qc_lots.sd": "质控品标准差",
    "spd_measurements.value": "慢专病体征测量值",
    "spd_indicators.target_value": "慢专病指标目标值（血压/糖化等，非金额）",
    "spd_targets.target_high": "慢专病控制目标上限",
    "spd_targets.target_low": "慢专病控制目标下限",
    "cold_chain_records.temperature": "冷链温度",
    "cold_chain_records.max_allowed": "冷链温度上限",
    "cold_chain_records.min_allowed": "冷链温度下限",
    "medical_wastes.weight_kg": "医废重量，公斤",
    # —— 剂量/用量：药学量纲 ——
    "drug_rules.max_daily_dose": "单日最大剂量",
    "drug_rules.ddd": "限定日剂量（DDD）",
    "prescription_items.daily_dose": "处方日剂量",
    # —— 权重/系数/比例/得分：无量纲，与 NOT_MONEY 同源 ——
    "drg_groups.base_weight": "DRG 权重，无量纲",
    "case_summaries.drg_weight": "病案 DRG 权重",
    "performance_indicators.weight": "绩效指标权重",
    "performance_formulas.weight": "绩效公式权重",
    "fund_distributions.weight": "分配份额权重",
    "fund_distributions.share_pct": "分配占比（百分数）",
    "fund_distributions.score": "分配评分",
    "cost_allocation_rules.ratio_pct": "成本分摊比例（百分数）",
    "payroll_records.perf_coefficient": "绩效系数，乘数不是金额",
    "spd_indicators.weight": "慢专病指标权重",
    "spd_scores.total_score": "慢专病考核综合得分（0-100）",
    "spd_assessments.score": "慢专病评估得分",
    "spd_screenings.score": "慢专病筛查得分",
    "spd_data_sources.success_rate": "对接成功率（百分比）",
    "training_records.score": "培训成绩",
    "training_assessments.score": "考核成绩",
}


def _all_float_columns() -> dict[str, sa.Column]:
    return {
        f"{t.name}.{c.name}": c
        for t in models.Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, sa.Float)
    }


def test_金额列一律定点数不用浮点():
    """浮点存金额的误差在阶段七露过头，国产库对浮点的处理又与 SQLite 不同。

    这条按名字查（词表命中即必须是定点数），下面那条按类型反查（Float 必须登记）。
    两条方向相反：名字对得上的走这条，名字对不上的也逃不掉下面那条。
    """
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


def test_每个Float列都必须登记为非金额():
    """反向闸门：分母是全部 Float 列，不是"名字里带钱字"的那些。

    新加一个 Float 列而没在 `FLOAT_NOT_MONEY` 里写明它为什么不是钱 → 当场红。
    这样"金额不得用 Float"这条纪律就不再依赖任何人记得给列起个带钱字的名字。
    """
    floats = _all_float_columns()
    unregistered = sorted(set(floats) - set(FLOAT_NOT_MONEY))
    by_words = {n for n in floats if any(w in n.split(".")[1] for w in MONEY_WORDS)}
    old_scope = by_words - set(NOT_MONEY)
    summary = (
        "\n[金额类型闸门] 覆盖面自证"
        f"\n  分母：模型元数据里全部 Float 列 {len(floats)} 个"
        f"（共 {len(models.Base.metadata.tables)} 张表 /"
        f" {sum(len(t.c) for t in models.Base.metadata.tables.values())} 列，全量、无抽样）"
        f"\n  已登记非金额：{len(set(FLOAT_NOT_MONEY) & set(floats))}    未登记：{len(unregistered)}"
        f"\n  对照：按命名词表（MONEY_WORDS {len(MONEY_WORDS)} 个词）时，"
        f"{len(floats)} 个 Float 列里只有 {len(by_words)} 个进得了分母、"
        f"扣掉已豁免的还剩 {len(old_scope)} 个真正被检查——"
        f"另外 {len(floats) - len(by_words)} 个连看都不会被看一眼"
    )
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert unregistered == [], (
        "以下 Float 列没有登记：\n  " + "\n  ".join(unregistered)
        + "\n\n金额一律用 models.Money（Numeric(14,2)）；确实不是金额的，"
        "加进 FLOAT_NOT_MONEY 并写明量纲——"
        "这不是走过场，`debit`/`credit`/`bonus` 就是当年靠命名词表漏掉的。"
    )


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


def test_浮点登记清单不许腐烂():
    """同上，作用在反向清单上：登记项必须对应一个真实存在的 Float 列。

    列改了类型（Float → Money）或改了名，登记条目必须一并删掉；否则将来
    同名的新列会被这条陈旧登记静默放行——正是这份清单最想防的那种事。
    """
    stale = sorted(set(FLOAT_NOT_MONEY) - set(_all_float_columns()))
    assert stale == [], (
        f"这些登记项已不是 Float 列（改类型/改名/删表），应从 FLOAT_NOT_MONEY 删除：{stale}"
    )


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
