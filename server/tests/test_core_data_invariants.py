"""核心数据的「不可变定义」——每个核心概念只有一个权威表，身份字段只归属它。

本仓库的头号技术债是"三套并行子域"：同一个概念（病种目录 / 患者 / 随访 / 转诊）
被慢病、专病、慢专病各造一套（见 docs/DATA_MODEL.md §2）。这类重复一旦落库就极难
收敛。本文件把**核心数据的定义钉成不可变**：核心概念有且只有一个权威表，核心身份
字段有且只归属那一个表——**以后不应随便再造一套**。

任何"新建一张患者主索引 / 又开一个机构层级表 / 另立一套账号体系"的改动，都会让下面
的断言变红，逼着走 ADR（docs/adr/）明确决策，而不是顺手复制。

要**扩展/变更**某个不可变定义（例如确实要在别处冗余存 id_card）：
先写 ADR，再把该表加进对应的 owners 白名单——一次被看见的决定，而非静默漂移。
"""
from __future__ import annotations

from app.main import app  # noqa: F401  触发全部模型 import（含 spd）
from app.database import Base

METADATA = Base.metadata


# 核心实体：概念 → 唯一权威表。这张表是该概念在全平台的**唯一定义**。
CANONICAL_ENTITIES: dict[str, str] = {
    "患者主索引": "patients",        # 全县唯一人物身份，别处一律引 patient_id
    "机构主数据": "organizations",   # 县-乡-村三级层级
    "员工账号": "users",             # 工作人员身份与鉴权
    "就诊记录": "encounters",        # 门急诊就诊事实
    "住院记录": "admissions",        # 住院事实
    "居民端账号": "resident_accounts",  # 居民端身份（手机号/微信，scope=portal）
}


# 身份标记字段 → 只允许归属的权威表。持有这些列 = 声称自己是那个核心身份的主。
# 现状：全部干净地只归属一处（id_card/ehc_no 只在 patients，别的表只引 patient_id）。
IDENTITY_OWNERS: dict[str, set[str]] = {
    "id_card": {"patients"},          # 身份证号：人物身份，存一处
    "ehc_no": {"patients"},           # 电子健康卡号：人物身份，存一处
    "org_type": {"organizations"},    # 机构类型：机构层级主数据
    "password_hash": {"users"},       # 口令散列：员工账号
    "token_valid_from": {"users"},    # 令牌基线：员工账号
}


def test_核心实体表都存在():
    missing = {concept: tbl for concept, tbl in CANONICAL_ENTITIES.items() if tbl not in METADATA.tables}
    assert not missing, f"核心实体权威表缺失：{missing}"


def test_身份字段只归属权威表():
    """每个身份标记字段的持有表集合，必须恰好等于白名单——不多不少。

    - 变多 = 有人另造了一套核心身份（如新建第二患者主索引）→ 挡下，逼走 ADR。
    - 变少 = 权威表被拆散了身份字段 → 同样是核心变更，需明示。
    """
    drift = {}
    for col, owners in IDENTITY_OWNERS.items():
        holders = {n for n, tb in METADATA.tables.items() if col in tb.c}
        if holders != owners:
            drift[col] = {"应为": sorted(owners), "实为": sorted(holders)}
    assert not drift, (
        f"核心身份字段归属发生变化：{drift}。"
        " 核心数据是不可变定义——新增/变更需先写 ADR（docs/adr/），再更新 IDENTITY_OWNERS 白名单。"
    )


def test_人物身份不得在别处另立():
    """更直白的一条：除 patients 外，任何表都不得引入 id_card 或 ehc_no 列。

    别处需要"这条记录属于谁"时，一律外键引 patient_id，而不是复制一份身份证号。
    这条既防重复身份主索引，也防 PII 明文四处扩散。
    """
    offenders = sorted(
        n for n, tb in METADATA.tables.items()
        if n != "patients" and ({"id_card", "ehc_no"} & set(tb.c.keys()))
    )
    assert not offenders, (
        f"以下表擅自引入了人物身份字段（应改为外键 patient_id）：{offenders}。"
    )
