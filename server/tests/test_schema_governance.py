"""数据模型治理——迁移纪律 · 核心表冻结 · 旧结构逐步迁移棘轮。

对应"优化数据库结构"八步里的可强制部分：
  ⑤ migration 体系   —— 双 head 纪律、零漂移守卫（表名级 + 真实结构 diff 棘轮）
  ⑥ 冻结核心表       —— 核心表列集合快照，任何增删改列即变红，逼走 ADR
  ⑦ 新需求按新标准   —— 新表必须带 created_at（欠账只减不增，见下）
  ⑧ 旧结构逐步迁移   —— created_at 欠账棘轮，只许变小

设计与 test_api_contract_governance 同源：不一次性重构，而是锁基线、只进不退。
数据结构的解读见 docs/DATA_MODEL.md；权威列表见 docs/schema/SCHEMA.md（自动生成）。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import warnings

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.main import app  # noqa: F401  触发全部模型 import（含 spd）
from app.database import Base

METADATA = Base.metadata
VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"


# ===========================================================================
# ⑤ migration 体系纪律
# ===========================================================================

def test_迁移恰有两个head_平台与spd():
    sd = ScriptDirectory.from_config(Config(str(VERSIONS.parent.parent / "alembic.ini")))
    heads = sd.get_heads()
    assert len(heads) == 2, f"应恰有 2 个 head（平台链 + spd 链），实为 {len(heads)}：{heads}"
    labels = set()
    for h in heads:
        labels |= set(sd.get_revision(h).branch_labels or ())
    assert "spd" in labels, "两个 head 里应有一个带 spd 分支标签"


def test_模型表零漂移_每张表都被迁移建过():
    # 迁移里 create_table 过的表名
    created: set[str] = set()
    for f in VERSIONS.glob("*.py"):
        created |= set(re.findall(r"create_table\(\s*[\"']([^\"']+)[\"']", f.read_text(encoding="utf-8")))
    model_tables = set(METADATA.tables)
    missing = model_tables - created
    assert not missing, (
        f"以下模型表没有对应的 create_table 迁移（漏写迁移，生产 PG 会缺表）：{sorted(missing)}。"
        " create_all 只在开发 SQLite 上掩盖此问题，见 CLAUDE.md §4。"
    )


# ===========================================================================
# ⑥ 冻结核心表——列集合快照
# ===========================================================================
# 核心主数据/身份表：几乎所有业务与 76 个 spd 外键都指向它们。冻结其**列集合**，
# 任何增/删/改名都会让下方断言变红——这不是禁止演进，而是逼着走 ADR（docs/adr/）
# 明确决策，而非顺手改。改列后：先写 ADR，再同步更新此快照。
FROZEN_CORE_COLUMNS: dict[str, list[str]] = {
    # E1 等保账户安全新增四列（status/password_updated_at/must_change_password/totp_secret），
    # 迁移 d4e5f6a7b8c1；ADR 由协调方随包合并补录（docs/ 属并行包禁改区）
    "users": ["created_at", "full_name", "id", "must_change_password", "org_id", "password_hash", "password_updated_at", "role", "status", "token_valid_from", "totp_secret", "username"],
    "organizations": ["address", "created_at", "id", "level", "name", "org_type", "parent_id"],
    # deactivated_at：个保法"删除权"落地为注销标记（工程包 E2，见 models/consent.py 与
    # routers/consents.py:review_correction）——注销不物理删除，检索/绑定入口过滤。
    # id_card_idx/phone_idx：PII 列加密的 HMAC 等值检索索引（工程包 E3，见 app/pii.py），
    # 迁移 a4b5c6d7e8f9；ADR 由协调方随包合并补录（docs/ 属并行包禁改区，同 E1 先例）。
    "patients": ["birth_date", "created_at", "deactivated_at", "ehc_no", "gender", "id", "id_card", "id_card_idx", "name", "phone", "phone_idx"],
    "encounters": ["created_at", "diagnosis_code", "diagnosis_name", "doctor_name", "encounter_type", "id", "org_id", "patient_id", "summary"],
    # created_at：ADR-0018（created_at 欠账收官）——行写入时间，与 admitted_at
    # 的业务时间区分；迁移 c1d2e3f4a5b6，历史行哨兵 1970 回填。
    "admissions": ["admitted_at", "bed_id", "created_at", "created_by", "diagnosis_name", "discharged_at", "doctor_name", "id", "org_id", "patient_id", "status", "ward_id"],
}


def test_核心表结构已冻结():
    drift = {}
    for name, frozen in FROZEN_CORE_COLUMNS.items():
        actual = sorted(METADATA.tables[name].c.keys())
        if actual != sorted(frozen):
            drift[name] = {"新增": sorted(set(actual) - set(frozen)), "移除": sorted(set(frozen) - set(actual))}
    assert not drift, (
        f"核心表结构发生变化：{drift}。核心表变更需先写 ADR（docs/adr/）明确决策，"
        " 再同步更新 FROZEN_CORE_COLUMNS 快照。"
    )


# ===========================================================================
# ⑦⑧ 审计字段：新表必须带 created_at，旧欠账只减不增
# ===========================================================================
# 246 张表里缺 created_at 的历史欠账。新增表必须带 created_at，老表逐步补——
# 本棘轮保证这个数字**只减不增**：新表漏 created_at 会顶破基线。
# 轨迹：52 → 51（voucher_entries）→ 50（fund_settlements）→ 48（maternal_visits + child_visits）
#      → 46（qc_records + visit_credentials）→ 45（spd_measurements，spd 链）
#      → 44（vaccination_records）→ 43（exam_reports）→ 42（health_monitor_records）
#      → 41（infectious_cases）→ 40（prescription_items）→ 39（drug_stocks）
#      → 38（appointment_slots）→ 37（duty_rosters）→ 36（departments）
#      → 32（consult_experts + drg_groups + report_templates + print_templates，配置类四表打包）
#      → 28（tcm_techniques + system_params + code_systems + code_entries，字典/参数类四表打包）
#      → 22（spd 规则/模板类六表打包，spd 链）→ 14（平台零散八表打包）
#      → 2（spd 其余配置/关联十二表收官批，spd 链）。仅剩 blood_stocks（降级）与
#      admissions（核心表，改列需先 ADR）。
#      → 0（2026-08-31 收官：blood_stocks 顺路补齐；admissions 走 ADR-0018 后补，
#      迁移 c1d2e3f4a5b6）。豁免清零——此后任何新表缺 created_at 都直接变红。
BASELINE_MISSING_CREATED_AT = 0


def test_缺created_at的表不许变多():
    missing = sorted(t for t, tb in METADATA.tables.items() if "created_at" not in tb.c)
    assert len(missing) <= BASELINE_MISSING_CREATED_AT, (
        f"缺 created_at 的表从基线 {BASELINE_MISSING_CREATED_AT} 涨到 {len(missing)}。"
        f" 新表必须带 created_at；新增的欠账：见列表 {missing}。"
    )
    if len(missing) < BASELINE_MISSING_CREATED_AT:
        print(f"\n[提示] created_at 欠账已降到 {len(missing)}，请把 BASELINE_MISSING_CREATED_AT 下调。")


# ===========================================================================
# ⑤·续 真实结构 diff 棘轮——"零漂移"守卫补上列/索引/唯一性/外键/可空性
# ===========================================================================
# 上面那条 `test_模型表零漂移_每张表都被迁移建过` 只用正则扒 `create_table("表名")`
# 比对**表名集合**：有表就算过。它从不看列、索引、唯一性、外键、可空性——
# 于是"零漂移"这个名字覆盖的分母，比它实际检查的东西大了一整圈
# （第 17 章例一：分母是"想到要测的东西"时，100% 能在漏掉半个天空的情况下算出来）。
#
# 实测：空库跑完 `alembic upgrade heads` 后用 alembic 自己的 `compare_metadata`
# 比对，SQLite 上 **75 处**差异、真 PG 上 **77 处**，一处都没被上面那条守卫发现。
# 其中三类都是会在生产上出事的：
#   * 18 个 spd 唯一索引被迁移建成**非唯一**——DB 级唯一约束根本不存在，
#     并发下"查了没有再插"照样插出两条（第 9 章那一家子缺陷的温床）；
#   * 14 个外键模型上有、迁移里没建——生产库不做参照完整性校验；
#   * 25 列模型 NOT NULL 而迁移建成可空——开发 SQLite 上 create_all 掩盖，
#     PG 上插入 NULL 会一路落库，读出来才炸。
#
# 处理方式与 test_api_contract_governance 同形：**不一次性修，先锁基线、只减不增**。
# 基线是实测差异快照（tests/snapshots/schema_drift_baseline.json），
# 每条按类别注明性质。新增差异（新写的迁移与模型对不上）会顶破基线变红；
# 修掉一处则提示把基线调小。欠账登记见 docs/TECH_DEBT.md。
#
# ⚠️ **不要为了让它绿而放宽断言**——放宽就退回成上面那条只看表名的空守卫。

DRIFT_BASELINE_PATH = pathlib.Path(__file__).resolve().parent / "snapshots" / "schema_drift_baseline.json"
SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]


def _fingerprint(entry) -> str:
    """把一条 alembic diff 压成**稳定、可读、与方言无关**的一行指纹。

    直接存 diff 对象没法比对（里面是 SQLAlchemy 对象、带内存地址），
    存自然语言又会因措辞变动而假红。指纹只取"哪张表、哪个对象、差在哪"。
    """
    kind = entry[0]
    if kind == "modify_nullable":
        _, _schema, table, column, _kw, db_nullable, model_nullable = entry
        return (
            f"modify_nullable | {table}.{column} | "
            f"迁移 nullable={db_nullable} → 模型 nullable={model_nullable}"
        )
    if kind in ("add_fk", "remove_fk"):
        fk = entry[1]
        cols = ",".join(c.name for c in fk.columns)
        target = sorted({e.target_fullname for e in fk.elements})
        table = fk.table.name if fk.table is not None else "?"
        return f"{kind} | {table}({cols}) → {'|'.join(target)}"
    if kind in ("add_index", "remove_index"):
        idx = entry[1]
        table = idx.table.name if idx.table is not None else "?"
        cols = ",".join(c.name for c in idx.columns)
        return f"{kind} | {table}.{idx.name}({cols}) | unique={bool(idx.unique)}"
    if kind in ("add_constraint", "remove_constraint"):
        con = entry[1]
        cols = ",".join(getattr(c, "name", str(c)) for c in con.columns)
        table = con.table.name if getattr(con, "table", None) is not None else (
            next(iter(con.columns)).table.name if len(con.columns) else "?"
        )
        return f"{kind} | {table}({cols}) | {type(con).__name__}"
    if kind in ("add_table", "remove_table"):
        return f"{kind} | {entry[1].name}"
    if kind in ("add_column", "remove_column"):
        return f"{kind} | {entry[2]}.{entry[3].name}"
    if kind in ("modify_type", "modify_default", "modify_comment"):
        return f"{kind} | {entry[2]}.{entry[3]} | {entry[5]!r} → {entry[6]!r}"
    return f"{kind} | {entry[1:]}"


def _collect_drift(url: str) -> tuple[list[str], dict[str, int]]:
    """空库 `alembic upgrade heads` → `compare_metadata` → 指纹列表 + 规模统计。

    走**子进程**跑迁移：alembic 的 env.py 会按 MEDPLAT_DATABASE_URL 建自己的
    engine，在当前进程里改环境变量会污染其它用例共用的 app.database.engine。
    升级用**复数 heads**（本仓库双 head，单数会报错且漏掉 spd 的 59 张表）。
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=SERVER_DIR,
        env={**os.environ, "MEDPLAT_DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"空库跑 alembic upgrade heads 失败：\n{result.stderr[-2000:]}"

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                # compare_type 打开：列类型不一致也算漂移。
                # compare_server_default 关闭：各方言把默认值渲染成不同字面量，
                # 打开会产生一批只在某个方言上成立的假差异（噪音会淹掉真问题）。
                opts={"compare_type": True, "compare_server_default": False},
            )
            raw = compare_metadata(ctx, METADATA)
    finally:
        engine.dispose()

    prints: list[str] = []
    for item in raw:
        # 列级修改会被包成 [(...)]，其余是裸 tuple
        for entry in item if isinstance(item, list) else [item]:
            prints.append(_fingerprint(entry))

    scale = {
        "表": len(METADATA.tables),
        "列": sum(len(t.c) for t in METADATA.tables.values()),
        "索引": sum(len(t.indexes) for t in METADATA.tables.values()),
        "外键": sum(len(t.foreign_keys) for t in METADATA.tables.values()),
    }
    return sorted(prints), scale


def _load_drift_baseline() -> dict:
    return json.loads(DRIFT_BASELINE_PATH.read_text(encoding="utf-8"))


def _report_drift(dialect: str, actual: list[str], scale: dict[str, int]) -> None:
    """棘轮判定 + **自证覆盖面**：比了多少对象、抓到多少差异、基线多少、清偿多少。

    第 17 章第 4 条：检查工具必须把"看了多少、跳过多少"打印出来。
    一个不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险。
    """
    baseline = _load_drift_baseline()
    expected = baseline["dialects"][dialect]["items"]
    new = sorted(set(actual) - set(expected))
    fixed = sorted(set(expected) - set(actual))

    checked = scale["列"] + scale["索引"] + scale["外键"]
    summary = "\n".join([
        "",
        f"[结构漂移棘轮 · {dialect}] 覆盖面自证",
        f"  比对对象：{scale['表']} 张表 / {scale['列']} 列 / {scale['索引']} 个索引 /"
        f" {scale['外键']} 个外键（共 {checked} 个可比对对象，全部纳入，无抽样、无跳过）",
        f"  实测差异：{len(actual)}    基线欠账：{len(expected)}    新增：{len(new)}    已清偿：{len(fixed)}",
        f"  分类：{json.dumps(_drift_kinds(actual), ensure_ascii=False)}",
        "  对照：只比表名的旧守卫在同一套库上抓到 0 处（分母只有表名，看不到列/索引/唯一性/外键）",
    ])
    print(summary)
    # print 在 `-q` 下会被吞掉；warning 会进"warnings summary"，让覆盖面数字
    # 在 CI 的默认输出里也看得见（自证覆盖面不能只在 -s 时才成立）。
    warnings.warn(summary, UserWarning, stacklevel=2)

    assert not new, (
        f"迁移与模型的结构差异变多了（{dialect}）：\n  " + "\n  ".join(new) +
        "\n新写的迁移必须与模型一致：列可空性、唯一索引、外键都要建出来。"
        "\n（确属无法消除的方言差异，才在 tests/snapshots/schema_drift_baseline.json 里登记并写明性质）"
    )
    if fixed:
        print(
            f"[提示] {dialect} 已清偿 {len(fixed)} 处结构漂移，请把 snapshots/schema_drift_baseline.json "
            f"对应条目删掉（棘轮只减不增）：\n  " + "\n  ".join(fixed)
        )


def _drift_kinds(items: list[str]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for line in items:
        kind = line.split(" | ")[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    return dict(sorted(kinds.items()))


def test_迁移与模型的真实结构差异只减不增_sqlite(tmp_path):
    """SQLite 档（CI 默认环境跑得到）：空库升到 heads 后与模型做真实结构 diff。

    SQLite 反射不出**无名唯一约束**，所以 PG 上多出的 2 处 remove_constraint
    在这里看不见——那 2 处由下面的 integration 档负责。快照里两个方言各存一份，
    差异性质写在 notes 里，不允许拿"SQLite 看不见"当作放过的理由。
    """
    actual, scale = _collect_drift(f"sqlite:///{tmp_path / 'drift.db'}")
    _report_drift("sqlite", actual, scale)


@pytest.mark.integration
def test_迁移与模型的真实结构差异只减不增_postgresql():
    """PG 档（`make test-integration`）：同一把尺子，更严格的一份基线。

    SQLite 的类型宽容与反射能力有限，会吞掉一部分差异；生产是 PG，
    "SQLite 绿了"从来不等于"PG 也对"（CLAUDE.md §6）。
    """
    pg_url = os.environ.get("MEDPLAT_PG_TEST_URL", "")
    if not pg_url:
        pytest.skip("需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL")
    from sqlalchemy import create_engine, text

    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    engine.dispose()

    actual, scale = _collect_drift(pg_url)
    _report_drift("postgresql", actual, scale)
