"""ORM 模型 · 个保法落地（工程包 E2）：知情同意采集、同意文本版本、更正/注销申请。

《个人信息保护法》要求"告知-同意"可举证、可撤回（第 14/15 条），个人享有
更正权与删除权（第 46/47 条）。平台此前只有调阅授权（ArchiveAuthorization，
授权**机构**调档案），没有"处理个人信息本身经过了同意"的记录。三张表分工：

- ConsentText       同意文本版本库：告知文本改一次就发一版。同意记录只存版本号，
                    事后要答得出"他当时同意的是哪段话"。
- ConsentRecord     同意采集记录：谁、在什么场景、对哪版文本、经什么方式表示了
                    同意；撤回置 revoked_at（不删——撤回本身也要可举证）。
- CorrectionRequest 更正/注销申请：居民端或窗口提交，director/admin 审核。
                    "删除权"实现为**注销**（置 patients.deactivated_at）而非物理
                    删除——医疗记录有法定保留义务，不因个人请求而销毁。

**与 clinical.py 的 ConsentTemplate/InformedConsent 不是重复子域**：那两张是
**医疗行为**知情同意（手术/麻醉/输血——同意的是"做这件医疗处置"），本模块是
**个人信息处理**同意（建档/入组/调阅——同意的是"收集与使用我的信息"）。
法律依据、场景枚举、撤回语义都不同，硬并成一套会让两边的取值范围互相污染；
但"模板改版、已签内容冻结在当时版本"的设计取自彼处，口径一致。

不放 core.py：core 是冻结核心表区（改列先 ADR），这三张是外围合规记录，
只以外键指向核心表；人物身份一律外键 patient_id（核心数据不可变定义）。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._base import utcnow


class ConsentText(Base):
    """同意文本版本：同一场景可有多版，active 标记当前对外展示/默认引用的那版。

    幂等种子只保证每个场景至少有一版默认文本；现场修订走"发新版"而不是改旧版，
    旧版必须原样保留——已按旧版取得的同意，其举证依据就是那段旧文本。
    """

    __tablename__ = "consent_texts"
    __table_args__ = (
        UniqueConstraint("scene", "version", name="uq_consent_text_scene_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 场景取值与 ConsentRecord.scene 同一套（见彼处注释）
    scene: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(1024))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConsentRecord(Base):
    """知情同意采集记录。

    撤回不删行：置 revoked_at。同意与撤回都是要件事实，删了就无从举证
    "撤回之前的那段处理是经过同意的"。
    """

    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # archive=建档, chronic_enroll=慢病入组, followup=随访, family_contract=家医签约,
    # cross_org_access=跨机构调阅, public_health_report=公卫上报,
    # family_delegate=家庭代管授权（居民端代管无手机号档案的第二因子，见 portal.py）
    scene: Mapped[str] = mapped_column(String(32), index=True)
    # 同意时引用的告知文本版本（ConsentText.version；窗口录入线下纸质版时可为纸面版本号）
    text_version: Mapped[str] = mapped_column(String(16), default="")
    # self=居民端本人自签, proxy=窗口代录
    method: Mapped[str] = mapped_column(String(16))
    # 窗口代录的经办人；居民自签为空
    operator_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 居民自签的账户；窗口代录为空
    resident_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("resident_accounts.id"), nullable=True
    )
    # 佐证材料：短信确认流水号 / 签字影像附件 id 等。窗口代录必填（接口校验非空）——
    # 代录没有佐证，事后就无法证明"同意"不是经办人替填的。
    evidence: Mapped[str] = mapped_column(String(256), default="")
    # 未成年人（<14 岁）登记同意时必填的监护人信息（接口校验）。
    # 列名刻意不叫 id_card：人物身份字段只归 patients（核心数据不可变定义），
    # 这里存的是监护人证件号，是同意要件的一部分而非第二套身份主索引。
    guardian_name: Mapped[str] = mapped_column(String(64), default="")
    guardian_id_card: Mapped[str] = mapped_column(String(18), default="")
    # 监护人与患者关系：father/mother/guardian/other
    guardian_relation: Mapped[str] = mapped_column(String(16), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CorrectionRequest(Base):
    """更正权 / 删除权（注销）申请：提交 → director/admin 审核 → 执行。

    - correction：通过时按 changes（JSON）更正 patients 的白名单字段
      （见 routers/consents.py 的 CORRECTABLE_FIELDS，**不含 id_card**）。
    - deactivate：通过时置 patients.deactivated_at（注销，不物理删除）。
    执行走写接口，由审计中间件（AuditLog）自然留痕。
    """

    __tablename__ = "correction_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # correction=字段更正, deactivate=档案注销（"删除权"的落地形态）
    request_type: Mapped[str] = mapped_column(String(16), default="correction", index=True)
    # 更正内容：{"字段名": "新值"} 的 JSON 串；deactivate 类申请为空
    changes: Mapped[str] = mapped_column(String(1024), default="")
    reason: Mapped[str] = mapped_column(String(256))
    # pending=待审核, approved=已通过, rejected=已拒绝
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # portal=居民端自提, window=机构窗口代提
    source: Mapped[str] = mapped_column(String(16))
    # 居民端自提的账户；窗口代提为空
    applicant_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("resident_accounts.id"), nullable=True
    )
    # 窗口代提的经办人；居民端自提为空
    applicant_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 审核人与审核意见（拒绝时必填意见，接口校验）
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(256), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
