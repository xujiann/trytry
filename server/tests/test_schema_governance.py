"""数据模型治理——迁移纪律 · 核心表冻结 · 旧结构逐步迁移棘轮。

对应"优化数据库结构"八步里的可强制部分：
  ⑤ migration 体系   —— 双 head 纪律、零漂移守卫
  ⑥ 冻结核心表       —— 核心表列集合快照，任何增删改列即变红，逼走 ADR
  ⑦ 新需求按新标准   —— 新表必须带 created_at（欠账只减不增，见下）
  ⑧ 旧结构逐步迁移   —— created_at 欠账棘轮，只许变小

设计与 test_api_contract_governance 同源：不一次性重构，而是锁基线、只进不退。
数据结构的解读见 docs/DATA_MODEL.md；权威列表见 docs/schema/SCHEMA.md（自动生成）。
"""
from __future__ import annotations

import pathlib
import re

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
    "users": ["created_at", "full_name", "id", "org_id", "password_hash", "role", "token_valid_from", "username"],
    "organizations": ["address", "created_at", "id", "level", "name", "org_type", "parent_id"],
    "patients": ["birth_date", "created_at", "ehc_no", "gender", "id", "id_card", "name", "phone"],
    "encounters": ["created_at", "diagnosis_code", "diagnosis_name", "doctor_name", "encounter_type", "id", "org_id", "patient_id", "summary"],
    "admissions": ["admitted_at", "bed_id", "created_by", "diagnosis_name", "discharged_at", "doctor_name", "id", "org_id", "patient_id", "status", "ward_id"],
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
# 当前 246 张表里 52 张缺 created_at（历史欠账）。新增表必须带 created_at，
# 老表逐步补——本棘轮保证这个数字**只减不增**：新表漏 created_at 会顶破基线。
BASELINE_MISSING_CREATED_AT = 52


def test_缺created_at的表不许变多():
    missing = sorted(t for t, tb in METADATA.tables.items() if "created_at" not in tb.c)
    assert len(missing) <= BASELINE_MISSING_CREATED_AT, (
        f"缺 created_at 的表从基线 {BASELINE_MISSING_CREATED_AT} 涨到 {len(missing)}。"
        f" 新表必须带 created_at；新增的欠账：见列表 {missing}。"
    )
    if len(missing) < BASELINE_MISSING_CREATED_AT:
        print(f"\n[提示] created_at 欠账已降到 {len(missing)}，请把 BASELINE_MISSING_CREATED_AT 下调。")
