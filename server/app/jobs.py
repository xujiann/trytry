"""内置定时任务实现（T1.1 / T1.3）。

每个任务是 `def job(db) -> (处理对象数, 结果摘要)`。约定：

- **查询口径复用既有预警接口的实现**，不在这里另写一套判定——否则同一件事
  "接口说超期 3 条、定时任务说 5 条"，谁都不敢信。
- 扫描类任务的产出是**广播 + 留痕**：把结果推给在线的管理端（WebSocket），
  并在 JobRun 里留下数量与摘要，供 `/api/jobs/runs` 回溯。
- 清理类任务直接删数据，删除量记进 affected。
- 归档类任务（A9）先导出 NDJSON.gz 并写 manifest，再删已归档行；
  归档目录固定在 `settings.upload_dir` 下的 archives/ 子目录。
"""
import gzip
import hashlib
import hmac
import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from .alerting import send_alert
from .clock import now_naive
from .config import settings
from .models import (
    AccessLog,
    AuditLog,
    ChronicPatient,
    FollowupTask,
    JobRun,
    MedicalWaste,
    SmsCode,
    StaffContract,
    TcmPreparationBatch,
)
from .scheduler import register
from .ws import manager

# 与 medwaste 路由同源的滞留天数上限
from .routers.medwaste import STORAGE_LIMIT_DAYS

# 合同/制剂的提前提醒窗口
CONTRACT_NOTICE_DAYS = 60
PREPARATION_NOTICE_DAYS = 30


def _alert(kind: str, title: str, count: int) -> None:
    """把扫描结果推给在线管理端；确定无人收到时转发 webhook 摘要兜底。

    工程包 P2：预警广播原是"无人在线即空操作"——夜间/节假日无人值守时，
    超期预警就此静默。broadcast 返回 False（无 Redis 总线且本进程无在线连接）
    时把摘要转发到运维告警 webhook（未配置 webhook 时仍是空操作，行为不变）。
    """
    if not count:
        return
    delivered = manager.broadcast({"type": kind, "title": title, "count": count})
    if not delivered:
        send_alert(f"unattended:{kind}", f"{title}：{count} 条（无在线管理端，广播未送达）")


@register("chronic_overdue_scan", "慢病随访超期扫描", 3600)
def chronic_overdue_scan(db: Session) -> tuple[int, str]:
    """随访超期名单：口径与 GET /api/chronic/overdue 一致。"""
    cutoff = date.today().isoformat()
    count = (
        db.query(ChronicPatient)
        .filter(ChronicPatient.next_due != "", ChronicPatient.next_due < cutoff)
        .count()
    )
    _alert("chronic_overdue", "慢病随访超期", count)
    return count, f"随访超期 {count} 例"


@register("medwaste_overdue_scan", "医废滞留扫描", 3600)
def medwaste_overdue_scan(db: Session) -> tuple[int, str]:
    """滞留预警：口径与 GET /api/medwaste/alerts 一致。"""
    cutoff = (date.today() - timedelta(days=STORAGE_LIMIT_DAYS)).isoformat()
    count = (
        db.query(MedicalWaste)
        .filter(MedicalWaste.status != "handed_over", MedicalWaste.collected_date <= cutoff)
        .count()
    )
    _alert("medwaste_overdue", "医废滞留超期", count)
    return count, f"医废滞留 {count} 批"


@register("contract_expiry_scan", "聘用合同到期提醒", 86400)
def contract_expiry_scan(db: Session) -> tuple[int, str]:
    """口径与 GET /api/mgmt/staff-contracts/expiring 一致（默认 60 天窗口）。"""
    deadline = (date.today() + timedelta(days=CONTRACT_NOTICE_DAYS)).isoformat()
    count = (
        db.query(StaffContract)
        .filter(StaffContract.status == "active", StaffContract.end_date <= deadline)
        .count()
    )
    _alert("contract_expiring", "聘用合同临期", count)
    return count, f"{CONTRACT_NOTICE_DAYS} 天内到期合同 {count} 份"


@register("preparation_expiry_scan", "中药制剂效期提醒", 86400)
def preparation_expiry_scan(db: Session) -> tuple[int, str]:
    """口径与 GET /api/tcm/preparation-batches/expiring 一致（默认 30 天窗口）。"""
    cutoff = (date.today() + timedelta(days=PREPARATION_NOTICE_DAYS)).isoformat()
    count = (
        db.query(TcmPreparationBatch)
        .filter(
            TcmPreparationBatch.status != "recalled",
            TcmPreparationBatch.expire_date != "",
            TcmPreparationBatch.expire_date <= cutoff,
        )
        .count()
    )
    _alert("preparation_expiring", "中药制剂临期", count)
    return count, f"{PREPARATION_NOTICE_DAYS} 天内到期制剂 {count} 批"


@register("followup_overdue_scan", "随访任务超期扫描", 3600)
def followup_overdue_scan(db: Session) -> tuple[int, str]:
    """口径与 GET /api/followups/overdue 一致，覆盖慢病/出院/术后/妇幼四类。"""
    cutoff = date.today().isoformat()
    count = (
        db.query(FollowupTask)
        .filter(FollowupTask.status == "pending", FollowupTask.due_date < cutoff)
        .count()
    )
    _alert("followup_overdue", "随访任务超期", count)
    return count, f"超期未随访 {count} 项"


@register("sms_code_cleanup", "过期验证码清理", 3600)
def sms_code_cleanup(db: Session) -> tuple[int, str]:
    """T1.3：过期或已消费的验证码即刻删除。

    留着没有任何用处——校验只认未过期未消费的最新一条——却让表无界增长，
    且过期验证码散列长期留存本身就是不必要的敏感数据暴露面。
    """
    now = now_naive()
    deleted = (
        db.query(SmsCode)
        .filter((SmsCode.expires_at <= now) | (SmsCode.consumed.is_(True)))
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted, f"清理过期/已用验证码 {deleted} 条"


# ---------------------------------------------------------------------------
# 运行数据保留期（A9）：JobRun 清理 + AccessLog/AuditLog 归档后截断
# ---------------------------------------------------------------------------

#: 归档分批大小：一批读多少行、写完就删掉并提交。防的是"一条大事务锁全表 +
#: 全表读进内存"——留痕表在生产是百万行级的。
ARCHIVE_BATCH_SIZE = 1000


def _archive_dir() -> Path:
    """归档目录：`settings.upload_dir` 下的 archives/ 子目录（不存在则建）。"""
    d = Path(settings.upload_dir) / "archives"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_manifest(entry: dict) -> None:
    """manifest.jsonl 追加一行。只追加不改写：manifest 本身就是归档的账本。"""
    path = _archive_dir() / "manifest.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _archive_and_delete(
    db: Session,
    model,
    table: str,
    cutoff,
    row_to_dict,
    anchors=None,
) -> tuple[int, str]:
    """把 `created_at < cutoff` 的行导出为 NDJSON.gz 后分批删除，写 manifest。

    - MAC 对**未压缩的 NDJSON 字节流**计算（HMAC-SHA256，密钥 settings.secret），
      校验方 `gunzip` 后即可复算，不依赖 gzip 实现的字节稳定性。
    - 每批"写文件→删行→commit"，进程中途挂掉最多损失一个未记入 manifest 的
      文件（行已删、文件还在），不会出现"行删了、导出没了"。
    - `anchors(first_rec, last_rec)` 收归档段首/尾行的**序列化字典**（ORM 实例在
      分批 commit 后会过期失效，锚点只能取自删除前的快照），供审计链记锚点。
    """
    base = (
        db.query(model)
        .filter(model.created_at < cutoff)
        .order_by(model.id)
    )
    first_batch = base.limit(ARCHIVE_BATCH_SIZE).all()
    if not first_batch:
        return 0, "无超期数据"

    stamp = now_naive().strftime("%Y%m%d%H%M%S")
    filename = f"{table}_{stamp}_{first_batch[0].id}.ndjson.gz"
    path = _archive_dir() / filename
    mac = hmac.new(settings.secret.encode(), digestmod=hashlib.sha256)
    total = 0
    first_rec: dict | None = None
    last_rec: dict | None = None
    ts_min = ts_max = None
    with gzip.open(path, "wb") as gz:
        rows = first_batch
        while rows:
            for row in rows:
                rec = row_to_dict(row)
                data = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
                gz.write(data)
                mac.update(data)
                if first_rec is None:
                    first_rec = rec
                last_rec = rec
                ts_min = rec["created_at"] if ts_min is None else min(ts_min, rec["created_at"])
                ts_max = rec["created_at"] if ts_max is None else max(ts_max, rec["created_at"])
            ids = [row.id for row in rows]
            db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            total += len(rows)
            # 游标推进用 id 而不是"删完了再查"：循环终止不依赖删除成败，
            # 删除异常时最多多归档、绝不死循环。
            rows = base.filter(model.id > ids[-1]).limit(ARCHIVE_BATCH_SIZE).all()

    assert first_rec is not None and last_rec is not None  # first_batch 非空必有值
    entry = {
        "file": filename,
        "table": table,
        "rows": total,
        "from": ts_min,
        "to": ts_max,
        "first_id": first_rec["id"],
        "last_id": last_rec["id"],
        "mac": mac.hexdigest(),
    }
    if anchors is not None:
        entry.update(anchors(first_rec, last_rec))
    _append_manifest(entry)
    return total, f"归档 {total} 行 → {filename}"


@register("esb_outbound_worker", "ESB 出站消息投递", 60)
def esb_outbound_worker(db: Session) -> tuple[int, str]:
    """周期消费 ESB 出站待投递消息（工程包 I1：集成层出站闭环）。

    口径与实现都在 routers/esb.py 的 `consume_pending_outbound`（与手工消费
    同一套投递/重试/死信/交换日志逻辑，不另写一份判定）：
    - 只消费出站端点（direction=outbound 且启用）的 queued 消息与到达
      next_retry_at 的 failed 消息，每轮上限 OUTBOUND_BATCH_SIZE 条（分批）；
    - 投递失败走既有指数退避重试，耗尽转死信；**告警交由日志**——
      本轮有失败时打 warning，值班按日志与 /api/esb/stats 追查。
    """
    import logging

    from .routers.esb import consume_pending_outbound

    processed, summary = consume_pending_outbound(db)
    if "失败 0" not in summary and processed:
        logging.getLogger("medplat.jobs").warning("[ESB] 出站投递存在失败：%s", summary)
    return processed, summary


@register("fhir_batch_export", "FHIR 批量导出（省平台前置机）", 3600)
def fhir_batch_export(db: Session) -> tuple[int, str]:
    """按增量水位把新增 Patient/Encounter/ExamReport 导出为 FHIR NDJSON（工程包 I1）。

    实现与序列化映射在 routers/integration.py 的 `run_fhir_batch_export`：
    文件落 `settings.upload_dir/fhir_out/`（含 manifest.jsonl），水位存
    system_params，重复执行幂等；供省平台前置机定期拉取。
    """
    from .routers.integration import run_fhir_batch_export

    return run_fhir_batch_export(db)


@register("jobrun_cleanup", "任务运行记录清理", 86400)
def jobrun_cleanup(db: Session) -> tuple[int, str]:
    """删除超过保留期（MEDPLAT_JOBRUN_RETENTION_DAYS，默认 90 天）的 JobRun。

    JobRun 只是运行留痕，不承载合规义务，到期直接删、不归档。
    保留期配成 0 或负值视为"未启用"，任务跳过并说明——保底防误配。
    """
    days = settings.jobrun_retention_days
    if days <= 0:
        return 0, f"保留期未启用（jobrun_retention_days={days}），跳过"
    cutoff = now_naive() - timedelta(days=days)
    deleted = (
        db.query(JobRun)
        .filter(JobRun.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted, f"清理 {days} 天前的任务运行记录 {deleted} 条"


@register("access_log_archive", "调阅留痕归档", 86400)
def access_log_archive(db: Session) -> tuple[int, str]:
    """把超过 MEDPLAT_ACCESS_LOG_ARCHIVE_DAYS 的 AccessLog 归档导出后删除。

    默认 0=不自动处理（留痕是《个保法》义务，删不删、留多久是运维决策）。
    导出为 NDJSON.gz + manifest.jsonl（含 HMAC 校验值），分批处理防大事务。
    """
    days = settings.access_log_archive_days
    if days <= 0:
        return 0, f"归档未启用（access_log_archive_days={days}），跳过"
    cutoff = now_naive() - timedelta(days=days)

    def row_to_dict(r: AccessLog) -> dict:
        return {
            "id": r.id,
            "user_id": r.user_id,
            "username": r.username,
            "org_id": r.org_id,
            "patient_id": r.patient_id,
            "resource": r.resource,
            "basis": r.basis,
            "created_at": r.created_at.isoformat(),
        }

    try:
        return _archive_and_delete(db, AccessLog, "access_logs", cutoff, row_to_dict)
    except Exception as exc:  # 工程包 P2：归档失败即留痕在丢失边缘，主动外发告警后照常上抛
        send_alert("archive_failed:access_logs", f"调阅留痕归档失败：{type(exc).__name__}: {exc}")
        raise


@register("audit_archive", "审计日志归档", 86400)
def audit_archive(db: Session) -> tuple[int, str]:
    """把超过 MEDPLAT_AUDIT_LOG_ARCHIVE_DAYS 的 AuditLog 归档导出后删除。

    默认 0=不自动处理，**开启是运维决策**（等保对审计留存有最低时限要求，
    截断前先确认归档存储已就位）。

    **哈希链截断说明**：audit_logs 带防篡改哈希链（audit_chain.py），删除最旧段
    后，库内剩余首条的 `prev_hash` 指向已被删除的记录——链首不再是空串，
    `GET /api/audit/verify` 会在**截断点报一处已知断点**（`partial_segment=true`，
    只能证明剩余段内部自洽）。这不是篡改，以归档 manifest 的锚点续接校验：
    manifest 里额外记录了归档段的 `first_prev_hash` / `first_entry_hash` /
    `last_entry_hash`，库内剩余首条的 `prev_hash` 应等于归档段的
    `last_entry_hash`，归档文件内部的链则可独立复算。详见运维手册
    "运行数据保留与归档"节。
    """
    days = settings.audit_log_archive_days
    if days <= 0:
        return 0, f"归档未启用（audit_log_archive_days={days}），跳过"
    cutoff = now_naive() - timedelta(days=days)

    def row_to_dict(r: AuditLog) -> dict:
        return {
            "id": r.id,
            "user_id": r.user_id,
            "username": r.username,
            "method": r.method,
            "path": r.path,
            "status_code": r.status_code,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "created_at": r.created_at.isoformat(),
        }

    def anchors(first: dict, last: dict) -> dict:
        # 链锚点：归档段首条的 prev/entry 哈希 + 尾条的 entry 哈希。
        # 库内剩余首条的 prev_hash == last_entry_hash 即证明链在截断点续接无误。
        return {
            "first_prev_hash": first["prev_hash"],
            "first_entry_hash": first["entry_hash"],
            "last_entry_hash": last["entry_hash"],
        }

    try:
        return _archive_and_delete(db, AuditLog, "audit_logs", cutoff, row_to_dict, anchors)
    except Exception as exc:  # 工程包 P2：审计归档失败涉及合规留存，主动外发告警后照常上抛
        send_alert("archive_failed:audit_logs", f"审计日志归档失败：{type(exc).__name__}: {exc}")
        raise
