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
import logging
from datetime import date, timedelta
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from .alerting import send_alert
from .audit_chain import anchor_mac
from .clock import now_naive
from .config import settings
from .egress import egress_url_allowed
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
from .pii import PII_PREFIX, decrypt_pii, pii_index
from .scheduler import register
from .ws import manager

logger = logging.getLogger("medplat.jobs")

# 与 medwaste 路由同源的滞留天数上限
from .routers.medwaste import STORAGE_LIMIT_DAYS

# 合同/制剂的提前提醒窗口
CONTRACT_NOTICE_DAYS = 60
PREPARATION_NOTICE_DAYS = 30

#: PII 索引自检的抽样行数上限（每列）。抽样而非全表：真要全量校对得把整列解密一遍，
#: 百万级患者库会把自检本身变成一次全库解密——抽样只为**发现**破损，修复靠
#: `scripts/pii_encrypt_backfill.py --rebuild-index` 全量重算。
PII_INDEX_SAMPLE_SIZE = 200
#: 自检覆盖的 (表, 明文列, 索引列)——与回填脚本 TARGETS 同一份清单
PII_INDEX_TARGETS = [
    ("patients", "id_card", "id_card_idx"),
    ("patients", "phone", "phone_idx"),
    ("resident_accounts", "phone", "phone_idx"),
]


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


def _index_ok(plain: str, stored_idx: str | None) -> bool:
    """库中索引是否算得对。轮换宽限期内旧钥索引同样算对——`pii_filter` 已按
    当前钥+旧钥双口径检索（见 app/pii.py），此时旧钥索引是**正常状态**而非破损，
    自检不该在整个宽限期里持续误报。"""
    if stored_idx == pii_index(plain):
        return True
    return bool(settings.secret_previous) and stored_idx == pii_index(
        plain, settings.secret_previous
    )


def check_pii_index_health(db: Session) -> list[str]:
    """PII 检索索引健康自检：返回问题描述列表（空 = 健康）。

    查两类破损，都以"零报错零日志地静默失效"为特征：

    1. **密文行索引为空**——迁移的索引回填带 ``NOT LIKE 'pii1$%'``，库已加密后
       重跑迁移（回滚重来、换库重建）会把密文行全部跳过，索引列留一片 NULL；
    2. **抽样解密重算与库中不符**——跑迁移的 shell 没导 MEDPLAT_SECRET，索引
       用默认密钥算出，非空但全错。

    两者的后果一样：等值检索命中 0，业务读成"这人没建过档"，于是重复建档、
    重复开户——主数据被静默污染，而唯一约束帮不上忙（密文列随机 nonce 永不
    冲突，idx 唯一索引因值不同也不冲突）。所以这条自检查的不是"检索慢了"，
    是**核心数据完整性**。

    修法固定：`python scripts/pii_encrypt_backfill.py --rebuild-index`。
    """
    problems: list[str] = []
    for table, plain_col, idx_col in PII_INDEX_TARGETS:
        missing = db.execute(
            text(
                f"SELECT count(*) FROM {table} "  # noqa: S608 - 表列名来自本文件常量
                f"WHERE {plain_col} LIKE :prefix AND {idx_col} IS NULL"
            ),
            {"prefix": PII_PREFIX + "%"},
        ).scalar_one()
        if missing:
            problems.append(f"{table}.{plain_col}：{missing} 行密文但检索索引为空")
        rows = db.execute(
            text(
                f"SELECT id, {plain_col} AS plain, {idx_col} AS idx FROM {table} "  # noqa: S608
                f"WHERE {idx_col} IS NOT NULL AND {plain_col} IS NOT NULL AND {plain_col} != '' "
                f"ORDER BY id DESC LIMIT :limit"
            ),
            {"limit": PII_INDEX_SAMPLE_SIZE},
        ).fetchall()
        mismatched = 0
        for row in rows:
            try:
                plain = decrypt_pii(str(row.plain))
            except ValueError:
                mismatched += 1  # 解不开的密文同样是"索引不可信"，一并计入
                continue
            if not _index_ok(plain, row.idx):
                mismatched += 1
        if mismatched:
            problems.append(
                f"{table}.{plain_col}：抽样 {len(rows)} 行中 {mismatched} 行索引与重算值不符"
            )
    return problems


#: 自检告警的 kind（启动期探针与定时任务共用，走同一条冷却）
PII_INDEX_ALERT_KIND = "pii_index_broken"
#: 自检发现问题时的统一修复指引
PII_INDEX_REMEDY = "修复：python scripts/pii_encrypt_backfill.py --rebuild-index"


def report_pii_index_health(db: Session) -> list[str]:
    """跑自检并把问题外发告警（无问题时零外呼）。返回问题列表。

    **不拒启、只告警**——与 config.py 里"多实例无 Redis 拒启"的先例是**不同**
    的取舍，理由写在这里免得日后看着不一致：

    - 那条拦的是**配置**错误（改个环境变量就能修），失效面是**安全**（已登出
      的令牌在别的实例仍然可用），必须在开始对外服务之前拦住；
    - 这条发现的是**数据面**状态（索引列的值），修复动作本身要求应用环境与
      真实密钥都在位（`--rebuild-index` 就在同一份代码同一份配置里跑）。
      拒启会让运维在一个起不来的实例上救火，还会把"证件号检索降级"升级成
      "整个县域平台不可用"——而此刻按 ehc_no / 姓名的检索、门诊住院医嘱、
      发药结算全都是好的，临床业务不该为一列索引停摆。

    所以：启动期探一次 + 每天定时探一次，问题走 alerting 的 webhook 推出去
    （未配 webhook 时退化为 ERROR 日志，与既有告警通道口径一致）。
    """
    problems = check_pii_index_health(db)
    if not problems:
        return problems
    message = "PII 检索索引异常，EMPI 去重与实名绑定可能静默失效：" + "；".join(problems)
    logger.error("[PII] %s。%s", message, PII_INDEX_REMEDY)
    send_alert(PII_INDEX_ALERT_KIND, f"{message}。{PII_INDEX_REMEDY}")
    return problems


@register("pii_index_health", "PII 检索索引自检", 86400)
def pii_index_health_scan(db: Session) -> tuple[int, str]:
    """日跑一次索引自检（口径见 `check_pii_index_health`）。"""
    problems = report_pii_index_health(db)
    if not problems:
        return 0, "PII 检索索引正常"
    return len(problems), "；".join(problems) + f"。{PII_INDEX_REMEDY}"


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
    from .routers.esb import consume_pending_outbound

    processed, summary = consume_pending_outbound(db)
    if "失败 0" not in summary and processed:
        logger.warning("[ESB] 出站投递存在失败：%s", summary)
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


# ---------------------------------------------------------------------------
# 审计链外部锚点（P1-21）
# ---------------------------------------------------------------------------

#: 锚点文件：与归档 manifest 同目录，同样 append-only。
ANCHOR_FILENAME = "audit_anchors.jsonl"
#: 锚点外发超时（秒）：与 alerting 同一姿势——旁路外呼宁可发不出去也不拖任务。
ANCHOR_WEBHOOK_TIMEOUT_SECONDS = 5.0


def _last_anchor_mac(path: Path) -> str:
    """锚点文件最后一行的 mac；无文件/空文件返回空串（链首）。"""
    if not path.exists():
        return ""
    last = ""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    if not last:
        return ""
    return str(json.loads(last).get("mac", ""))


def _post_anchor(record: dict) -> str:
    """锚点外发异机存证；返回结果摘要片段。失败仅 log——本地锚点已落盘。"""
    url = settings.audit_anchor_webhook_url
    if not url:
        return ""
    if not egress_url_allowed(url, "MEDPLAT_AUDIT_ANCHOR_WEBHOOK_URL"):
        return "；webhook 未过出网校验，未外发"
    try:
        httpx.post(url, json=record, timeout=ANCHOR_WEBHOOK_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 外发是旁路，失败不打断任务，下轮锚点会再发
        logger.error("[AUDIT] 锚点 webhook 外发失败（本地锚点已写入），本条存证缺异机副本",
                     exc_info=True)
        return "；webhook 外发失败（见日志）"
    return "；已外发异机存证"


@register("audit_anchor", "审计链外部锚点", 86400)
def audit_anchor(db: Session) -> tuple[int, str]:
    """把审计链尾锚定到库外（P1-21）。

    库内哈希链发现得了"历史记录被改"，发现不了"末尾删掉 N 条"——删尾之后
    剩余链照样自洽。日跑一次，把链尾（最新带哈希行的 id / entry_hash + 全表
    行数）写进 `upload_dir/archives/audit_anchors.jsonl`：截断后锚点所指的行
    不在库里，`GET /api/audit/verify?anchor_id=&anchor_hash=` 对账即暴露。

    - 锚点文件自身成链：每行含对上一行 mac 的 HMAC（audit_chain.anchor_mac，
      独立用途派生密钥），删改锚点行同样看得出来；
    - `MEDPLAT_AUDIT_ANCHOR_WEBHOOK_URL` 非空时同步 POST 外发（经 egress
      出网校验，5s 超时，失败仅 log）——异机副本才是对"库+盘都有权限者"的
      真正防线，本地锚点链防的是"顺手删几行"。
    """
    tail = (
        db.query(AuditLog)
        .filter(AuditLog.entry_hash != "")
        .order_by(AuditLog.id.desc())
        .first()
    )
    if tail is None:
        return 0, "库内没有带哈希的审计记录，无链可锚"
    total = db.query(AuditLog).count()
    record = {
        "at": now_naive().isoformat(),
        "tail_id": tail.id,
        "tail_entry_hash": tail.entry_hash,
        "total_rows": total,
    }
    path = _archive_dir() / ANCHOR_FILENAME
    prev_mac = _last_anchor_mac(path)
    record["prev_mac"] = prev_mac
    record["mac"] = anchor_mac(prev_mac, record)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    posted = _post_anchor(record)
    return 1, f"锚定链尾 id={tail.id}（全表 {total} 行）{posted}"
