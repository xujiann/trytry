"""内置定时任务实现（T1.1 / T1.3）。

每个任务是 `def job(db) -> (处理对象数, 结果摘要)`。约定：

- **查询口径复用既有预警接口的实现**，不在这里另写一套判定——否则同一件事
  "接口说超期 3 条、定时任务说 5 条"，谁都不敢信。
- 扫描类任务的产出是**广播 + 留痕**：把结果推给在线的管理端（WebSocket），
  并在 JobRun 里留下数量与摘要，供 `/api/jobs/runs` 回溯。
- 清理类任务直接删数据，删除量记进 affected。
"""
from datetime import date, timedelta

from sqlalchemy.orm import Session

from .clock import now_naive
from .models import (
    ChronicPatient,
    FollowupTask,
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
    """把扫描结果推给在线管理端；无人在线时是空操作。"""
    if count:
        manager.broadcast({"type": kind, "title": title, "count": count})


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
