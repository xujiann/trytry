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

## 内置采集器：`publichealth`

`COLLECTORS` 里目前只有这一个（`collect_publichealth`）：把平台公卫随访
（`followups` 表）里结构化的血压/血糖同步成 `spd_measurements`。
这不是"对接外部系统"，而是**同库内的口径转换**：平台按业务存（随访），
慢专病按指标存（`metric` + `value` + 等级判定）。做成采集器而不是每次现算，
是因为指标等级要按**采集当时**的管理目标固化（见 `SpdMeasurement.level` 的注释）。

另有 `collect_encounter_probe`——**它是探针不是采集器，一行都不落库，且刻意没有
注册进 `COLLECTORS`**。它曾顶着 HIS/EMR 的名字注册着，于是监控页显示"运行正常、
本次 N 行"而实际一条数据都没进来；现在那两个源类型如实显示"未注册采集器"
（`unregistered_types()`）。详见该函数自己的 docstring。

真实的 HIS/LIS 采集器由实施期按各县接口补进 `COLLECTORS`——注册一个函数即可，
不需要改调度、监控与告警。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func
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


def lookback_since(db: Session, source: SpdDataSource) -> datetime:
    """采集的回溯窗口起点：上一次**成功**同步的开始时刻再往前放一个周期，且至少 `freq_minutes × 4`（P2-254）。

    原先只取 `freq_minutes × 4`，说是「比同步周期宽一倍以上，一次漏跑补得回来」——可同步周期不是 `freq_minutes`：
    定时任务按 5 分钟唤醒（`spd_data_source_sync`），1 分钟一次的源实际 5 分钟才跑一回，4 分钟的窗口每轮漏掉
    头 1 分钟里录的随访（约 20%）；定时任务停过、实例重启、某一轮采集失败，窗口移过去就再也补不回来。从上一次成功
    同步算起，漏跑、失败都补得回来；幂等键（`source_ref`）保证补跑不重复。从没成功过的新源照旧只看 `× 4`。"""
    period = timedelta(minutes=max(source.freq_minutes, 1))
    floor = now_naive() - period * 4
    last_ok = (
        db.query(func.max(SpdSyncLog.started_at))
        .filter(SpdSyncLog.source_id == source.id, SpdSyncLog.success.is_(True))
        .scalar()
    )
    return floor if last_ok is None else min(floor, last_ok - period)


#: 公卫随访 → 慢专病指标：（指标, 随访上的列, 单位, 判等级用的病种）
_PUBLICHEALTH_METRICS = (
    ("bp_sys", "sbp", "mmHg", "hypertension"),
    ("bp_dia", "dbp", "mmHg", "hypertension"),
    ("glucose_fasting", "glucose", "mmol/L", "diabetes"),
)
#: 公卫随访一页取多少条（判重的 IN 列表至多三倍于它）
PUBLICHEALTH_PAGE = 500


def collect_publichealth(db: Session, source: SpdDataSource) -> int:
    """公卫慢病随访 → 慢专病监测数据（P1-1 第一个真实采集器）。

    平台的公卫随访（`followups` 表）里有结构化的血压/血糖，慢专病按指标存
    （`metric` + `value` + 等级）。这是**同库内的口径转换**：

    - 等级按**采集当时**的管理目标固化（`judge_measurement`），不是显示时现算；
    - `source_ref = chronic_fu:{id}:{metric}` 兼作幂等键——重复同步不产生重复行；
    - 病种编码按数值指标推断（血压→hypertension、血糖→diabetes）：等级判定
      需要病种上下文，公卫随访没带病种时按指标语义取最常见的目标口径。

    回溯窗口从上一次**成功**同步算起（`lookback_since`，至少 `freq_minutes × 4`）：漏跑、失败都补得回来，
    幂等键保证补跑不重复。

    窗口按 id 翻页**取完**（每页 `PUBLICHEALTH_PAGE` 条）。原先只取窗口里最早的一页：窗口里多于一页时，
    每一轮取到的都是同一批最早的——头一轮落库、之后全被判重跳过——其余的一轮都轮不到，窗口移过去就永远
    出了窗口，同步日志照写成功（P2-94）。判重只查这一页的来源键；原先每轮把全部监测值的来源键读进内存。
    """
    since = lookback_since(db, source)
    written, after_id = 0, 0
    while True:
        rows = iter_recent_chronic_followups(db, since, limit=PUBLICHEALTH_PAGE, after_id=after_id)
        if not rows:
            return written
        refs = [f"chronic_fu:{followup.id}:{metric}" for followup, _ in rows for metric, *_ in _PUBLICHEALTH_METRICS]
        existing = {
            ref for (ref,) in db.query(SpdMeasurement.source_ref).filter(SpdMeasurement.source_ref.in_(refs)).all()
        }
        for followup, patient_id in rows:
            for metric, attr, unit, program in _PUBLICHEALTH_METRICS:
                value = getattr(followup, attr)
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
        if len(rows) < PUBLICHEALTH_PAGE:
            return written
        after_id = rows[-1][0].id


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
        # 每个源的采集圈在自己的保存点里（P2-265）：原先只接住了异常——采集器里撞了库（约束、方言、连接），会话随之
        # 作废，下面写同步日志那一下就抛 PendingRollbackError，整轮同步连同别的源已采的一起回滚、谁也不留日志，
        # 与这里「一个数据源挂掉不该拖垮整轮同步」的本意相反。退回保存点只丢这个源的半截写入
        savepoint = db.begin_nested()
        try:
            rows = collector(db, source)
            db.flush()   # 采集器挂起的写入在保存点里落，出错也在这一层退
        except Exception as exc:  # noqa: BLE001 - 一个数据源挂掉不该拖垮整轮同步
            savepoint.rollback()
            rows, success, message = 0, False, str(exc)[:200]
            logger.exception("数据源采集失败：%s", source.code)
        else:
            savepoint.commit()   # 释放保存点（不释放会一层层压在外层事务上，见 concurrency.insert_if_absent）

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
