"""数据源采集器：把 `spd_data_sources` 从"接入登记表"变成真的会拉数的东西。

招标文件平台管理端 #14/#15 要的是两件事——**对接**（HIS/EMR/LIS/体检/公卫）
与**监控**（成功时间、数据量、延迟、成功率、运行状态）。监控那半已经有了
（`POST /api/spd/data-sources/{id}/sync-logs` + `GET /api/spd/data-sources-monitor`），
这里补上采集那半的骨架与一个可用的内置实现。

## 结构

一个数据源有 `source_type`，按类型找采集器：

    collector = COLLECTORS.get(source.source_type)
    rows = collector(db, source)   # 返回本次落库的行数

采集器只管"取数 + 落进慢专病自己的表"，**同步日志由 `run_source` 统一写**——
让每个采集器自己记日志，迟早出现某个采集器忘了记，而监控页看到的是"一切正常"。

## 内置采集器：`internal`

从平台自身的就诊与住院数据里，把慢专病要用的体征/检验值同步成 `spd_measurements`。
这不是"对接外部系统"，而是**同库内的口径转换**：平台按业务存（就诊、检验申请），
慢专病按指标存（`metric` + `value` + 等级判定）。做成采集器而不是每次现算，
是因为指标等级要按**采集当时**的管理目标固化（见 `SpdMeasurement.level` 的注释）。

真实的 HIS/LIS 采集器由实施期按各县接口补进 `COLLECTORS`——注册一个函数即可，
不需要改调度、监控与告警。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from ..clock import now_naive
from .models import SpdDataSource, SpdMeasurement, SpdSyncLog
from .platform import Encounter, iter_recent_chronic_followups
from .service import judge_measurement

logger = logging.getLogger("medplat.spd.collectors")

#: 采集器签名：(db, source) -> 本次落库行数
Collector = Callable[[Session, SpdDataSource], int]


def collect_encounter_probe(db: Session, source: SpdDataSource) -> int:
    """**探针**（不是采集器）：数一数本机构近期有多少条就诊记录，返回条数。

    它一行都不落库。之所以留着，是因为"这个源的连通性与数据量看起来正常吗"
    在实施期确实要看一眼；但它**不再注册给 HIS / EMR**——那两个源类型现在
    如实显示"未注册采集器"（`unregistered_types()` 会把它们列进实施待办，
    监控页也会告警）。

    此前它顶着 HIS/EMR 的名字注册着，于是监控页显示"运行正常、本次 N 行"，
    而实际一行数据都没进来。**看起来是好的**比"明摆着没接"更危险：
    没接会有人去接，看起来好的没人会去查。
    """
    since = now_naive() - timedelta(minutes=max(source.freq_minutes, 1) * 2)
    query = db.query(Encounter).filter(Encounter.created_at >= since)
    if source.org_id is not None:
        query = query.filter(Encounter.org_id == source.org_id)
    return query.count()


def collect_publichealth(db: Session, source: SpdDataSource) -> int:
    """公卫慢病随访 → 慢专病监测数据（P1-1 第一个真实采集器）。

    平台的公卫随访（`followups` 表）里有结构化的血压/血糖，慢专病按指标存
    （`metric` + `value` + 等级）。这是**同库内的口径转换**：

    - 等级按**采集当时**的管理目标固化（`judge_measurement`），不是显示时现算；
    - `source_ref = chronic_fu:{id}:{metric}` 兼作幂等键——重复同步不产生重复行；
    - 病种编码按数值指标推断（血压→hypertension、血糖→diabetes）：等级判定
      需要病种上下文，公卫随访没带病种时按指标语义取最常见的目标口径。

    回溯窗口取 `freq_minutes × 4`：比同步周期宽一倍以上，一次漏跑补得回来，
    幂等键保证补跑不重复。
    """
    since = now_naive() - timedelta(minutes=max(source.freq_minutes, 1) * 4)
    rows = iter_recent_chronic_followups(db, since)
    existing = {
        ref for (ref,) in db.query(SpdMeasurement.source_ref)
        .filter(SpdMeasurement.source_ref != "").all()
    }
    written = 0
    for followup, patient_id in rows:
        for metric, value, unit, program in (
            ("bp_sys", followup.sbp, "mmHg", "hypertension"),
            ("bp_dia", followup.dbp, "mmHg", "hypertension"),
            ("glucose_fasting", followup.glucose, "mmol/L", "diabetes"),
        ):
            if value is None:
                continue
            ref = f"chronic_fu:{followup.id}:{metric}"
            if ref in existing:
                continue
            level = judge_measurement(db, program, "", metric, value)
            db.add(
                SpdMeasurement(
                    patient_id=patient_id, program_code=program, metric=metric,
                    value=float(value), unit=unit, level=level,
                    source="publichealth", source_ref=ref,
                    measured_at=followup.created_at,
                )
            )
            existing.add(ref)
            written += 1
    return written


#: 已实现的采集器。**只登记真的会落库的实现**——把探针挂上来会让监控页
#: 显示"正常"，而那正是最难发现的一种坏。HIS / EMR / LIS / 体检要等实施期
#: 拿到院内接口再注册（`register_collector`），在那之前它们如实显示"未注册"。
COLLECTORS: dict[str, Collector] = {
    "publichealth": collect_publichealth,
}


def register_collector(source_type: str, collector: Collector) -> None:
    """实施期按县接入真实系统时注册自己的采集器。"""
    COLLECTORS[source_type] = collector


def run_source(db: Session, source: SpdDataSource) -> SpdSyncLog:
    """跑一个数据源并写同步日志，同时刷新监控冗余列。

    成败都写日志：失败不写日志，监控页会显示"最近一次同步很久以前"，
    而看不出到底是没跑还是跑挂了——这两件事的处置完全不同。
    """
    started = now_naive()
    collector = COLLECTORS.get(source.source_type)
    rows, success, message = 0, True, ""
    if collector is None:
        success, message = False, f"未注册 {source.source_type} 采集器"
    else:
        try:
            rows = collector(db, source)
        except Exception as exc:  # noqa: BLE001 - 一个数据源挂掉不该拖垮整轮同步
            success, message = False, str(exc)[:200]
            logger.exception("数据源采集失败：%s", source.code)

    latency_ms = int((now_naive() - started).total_seconds() * 1000)
    log = SpdSyncLog(
        source_id=source.id, started_at=started, rows=rows,
        latency_ms=latency_ms, success=success, message=message,
    )
    db.add(log)
    db.flush()

    recent = (
        db.query(SpdSyncLog.success)
        .filter(SpdSyncLog.source_id == source.id)
        .order_by(SpdSyncLog.id.desc())
        .limit(100)
        .all()
    )
    ok = sum(1 for (flag,) in recent if flag)
    source.success_rate = round(ok / len(recent) * 100, 2) if recent else 0.0
    source.last_sync_at = started
    source.last_rows = rows
    source.last_latency_ms = latency_ms
    source.status = (
        "failed" if not success
        else "delayed" if latency_ms > source.freq_minutes * 60 * 1000
        else "running"
    )
    return log


def run_due_sources(db: Session) -> tuple[int, str]:
    """跑一遍到期的数据源，供定时任务调用。返回 (处理数, 摘要)。

    "到期"按各源自己的 `freq_minutes` 判定，而不是所有源一起跑：
    HIS 可能要 5 分钟一次，体检系统一天一次就够，混在一个周期里必然有一头不合适。
    """
    now = now_naive()
    sources = db.query(SpdDataSource).filter(SpdDataSource.active.is_(True)).all()
    due = [
        s for s in sources
        if s.last_sync_at is None
        or (now - s.last_sync_at) >= timedelta(minutes=max(s.freq_minutes, 1))
    ]
    failed = 0
    for source in due:
        log = run_source(db, source)
        if not log.success:
            failed += 1
    summary = f"到期数据源 {len(due)} 个，失败 {failed} 个" if due else "没有到期的数据源"
    return len(due), summary


def unregistered_types(db: Session) -> list[str]:
    """已登记但没有采集器的源类型——实施期的待办清单，也是监控页的告警来源。"""
    types = {
        s.source_type
        for s in db.query(SpdDataSource).filter(SpdDataSource.active.is_(True)).all()
    }
    return sorted(t for t in types if t not in COLLECTORS)
