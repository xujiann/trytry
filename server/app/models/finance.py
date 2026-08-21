"""ORM 模型 · 财务与医保：收费结算、基金、会计凭证、成本、薪酬、DRG。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import Money, utcnow


class InsuranceSettlement(Base):
    """⑲医保业务协同：结算记录（本地/异地）。"""

    __tablename__ = "insurance_settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # local=本地结算, remote=异地结算
    settle_type: Mapped[str] = mapped_column(String(16), default="local")
    total_amount: Mapped[float] = mapped_column(Money)
    insurance_pay: Mapped[float] = mapped_column(Money)
    self_pay: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinanceEntry(Base):
    """㉛财务统一协同管理：独立建账、集中核算。"""

    __tablename__ = "finance_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    # income=收入, expense=支出
    category: Mapped[str] = mapped_column(String(8))
    item: Mapped[str] = mapped_column(String(128), default="")
    amount: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChargeItem(Base):
    """收费项目目录：价格管理与公示，编码关联四统一 charge 字典。"""

    __tablename__ = "charge_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # drug=药品, exam=检查检验, treatment=治疗处置, bed=床位, other=其他
    category: Mapped[str] = mapped_column(String(16), default="other")
    price: Mapped[float] = mapped_column(Money)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BillDetail(Base):
    """费用明细：门诊按就诊（encounter）、住院按住院登记（admission）累计。"""

    __tablename__ = "bill_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    admission_id: Mapped[int | None] = mapped_column(
        ForeignKey("admissions.id"), nullable=True, index=True
    )
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True
    )
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    # 计费时价格快照（此后目录调价不影响已计费明细）
    unit_price: Mapped[float] = mapped_column(Money)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Money)
    # 结算后回填：未结清明细 settlement_id 为空
    settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("settlements.id"), nullable=True, index=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Settlement(Base):
    """结算单：汇总未结清明细→医保分担（联动 InsuranceSettlement）→结清。"""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # outpatient=门诊结算, inpatient=住院（出院）结算
    bill_type: Mapped[str] = mapped_column(String(16))
    admission_id: Mapped[int | None] = mapped_column(
        ForeignKey("admissions.id"), nullable=True, index=True
    )
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True
    )
    total_amount: Mapped[float] = mapped_column(Money)
    insurance_pay: Mapped[float] = mapped_column(Money, default=0)
    self_pay: Mapped[float] = mapped_column(Money, default=0)
    # 关联医保结算记录（复用 insurance 域，insurance_pay>0 时生成）
    insurance_settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("insurance_settlements.id"), nullable=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DrgGroup(Base):
    """DRG 分组目录：编码/名称/MDC/基准权重/主诊断关键词/主手术关键词。"""

    __tablename__ = "drg_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    base_weight: Mapped[float] = mapped_column(Float)
    # 主诊断匹配关键词（逗号分隔），出院病例按关键词命中入组
    keywords: Mapped[str] = mapped_column(String(256), default="")
    # 块3：主要诊断大类（MDCB/MDCE/... ），供 DRG 统计按 MDC 汇总
    mdc: Mapped[str] = mapped_column(String(8), default="", index=True)
    mdc_name: Mapped[str] = mapped_column(String(64), default="")
    # 块3：主手术关键词（逗号分隔），与病案首页 operation 匹配
    procedure_keywords: Mapped[str] = mapped_column(String(256), default="")
    # 块3：True=外科操作组，未命中主手术不得入组
    require_procedure: Mapped[bool] = mapped_column(Boolean, default=False)
    # 块3：QY 兜底组标志，统计中单列且不计入 CMI
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PayrollRecord(Base):
    """㉚薪酬福利管理 / ㉟绩效薪酬分配：月度薪酬 = 基础 + 绩效×系数。"""

    __tablename__ = "payroll_records"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_payroll_emp_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    base_salary: Mapped[float] = mapped_column(Money)
    # 绩效奖金也是金额，同样漏在阶段十二第一遍之外（同表的 base_salary/total
    # 都已是 Money，唯独它不是——按命名批量改最典型的漏法）
    perf_bonus: Mapped[float] = mapped_column(Money, default=0)
    # 绩效系数（考核结果联动薪酬分配）
    perf_coefficient: Mapped[float] = mapped_column(Float, default=1.0)
    total: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Budget(Base):
    """㉛预算管理：年度收支预算编制与执行对比。"""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("org_id", "year", "category", name="uq_budget_org_year_cat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    year: Mapped[str] = mapped_column(String(4), index=True)
    # income=收入预算, expense=支出预算
    category: Mapped[str] = mapped_column(String(8))
    amount: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentOrder(Base):
    """支付单：一次结算可分多笔渠道支付（现金/银行卡/医保/线上）。

    trade_no 为支付通道返回的外部流水号，是日终对账的比对主键。
    """

    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), index=True)
    # cash=现金, card=银行卡, insurance=医保基金, online=线上支付
    channel: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    # pending=待支付, paid=已支付, refunded=已全额退款, failed=支付失败
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    trade_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 已退金额（部分退款累计；等于 amount 时状态转 refunded）
    refunded_amount: Mapped[float] = mapped_column(Money, default=0)
    fail_reason: Mapped[str] = mapped_column(String(256), default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ReconciliationBatch(Base):
    """日终对账单：某自然日本地支付单与通道流水的比对结果汇总。"""

    __tablename__ = "reconciliation_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    unmatched: Mapped[int] = mapped_column(Integer, default=0)
    diff_amount: Mapped[float] = mapped_column(Money, default=0)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReconciliationDiff(Base):
    """对账差异明细：本地有通道无 / 通道有本地无 / 金额不一致。"""

    __tablename__ = "reconciliation_diffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    # 通道单边流水（missing_local）无本地支付单，order_id 为空
    order_id: Mapped[int | None] = mapped_column(ForeignKey("payment_orders.id"), nullable=True)
    trade_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # missing_local=通道有本地无, missing_remote=本地有通道无, amount_mismatch=金额不一致
    diff_type: Mapped[str] = mapped_column(String(20), index=True)
    local_amount: Mapped[float] = mapped_column(Money, default=0)
    remote_amount: Mapped[float] = mapped_column(Money, default=0)
    detail: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccountSubject(Base):
    """会计科目。

    此前 FinanceEntry 只是"期间 + 收/支 + 金额"的流水台账，出不了资产负债表，
    也无法核对借贷是否平衡。这里补上科目体系与凭证，FinanceEntry 保留为
    业务口径的收支汇总，二者并存不互相取代。
    """

    __tablename__ = "account_subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # asset=资产, liability=负债, net_asset=净资产, income=收入, expense=费用
    category: Mapped[str] = mapped_column(String(16), index=True)
    # 余额方向：debit=借方增加（资产/费用），credit=贷方增加（负债/净资产/收入）
    direction: Mapped[str] = mapped_column(String(8), default="debit")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Voucher(Base):
    """记账凭证：草稿可改，过账后锁定，冲销走作废而不是删除。"""

    __tablename__ = "vouchers"
    __table_args__ = (UniqueConstraint("org_id", "voucher_no", name="uq_voucher_org_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    voucher_no: Mapped[str] = mapped_column(String(32))
    voucher_date: Mapped[str] = mapped_column(String(10), index=True)
    summary: Mapped[str] = mapped_column(String(256), default="")
    total_debit: Mapped[float] = mapped_column(Money, default=0)
    total_credit: Mapped[float] = mapped_column(Money, default=0)
    # draft=草稿, posted=已过账, void=已作废
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    entries: Mapped[list["VoucherEntry"]] = relationship(back_populates="voucher")


class VoucherEntry(Base):
    """凭证分录：一条分录只能是借方或贷方，不能两边都填。"""

    __tablename__ = "voucher_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("vouchers.id"), index=True)
    subject_code: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(String(256), default="")
    # 借贷金额：整个复式记账、试算平衡表与合并报表都靠这两列求和。
    # 它们不含 amount/price/cost 这类词，阶段十二第一遍按命名族批量改类型时
    # 漏掉了——**平台最核心的两个金额列反而是最后改的**。教训写在这里：
    # 按命名批量处理必须回头核对剩下的清单，不能只看匹配到的那一批。
    debit: Mapped[float] = mapped_column(Money, default=0)
    credit: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    voucher: Mapped[Voucher] = relationship(back_populates="entries")


class DepartmentCost(Base):
    """科室成本归集：期间 × 科室 × 成本项的直接成本。"""

    __tablename__ = "department_costs"
    __table_args__ = (
        UniqueConstraint("dept_id", "period", "cost_type", name="uq_dept_cost_period_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    # labor=人员经费, drug=药品, consumable=卫生材料, depreciation=折旧, overhead=其他运行
    cost_type: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CostAllocationRule(Base):
    """成本分摊规则：行政后勤/医技科室的成本按比例分摊到临床科室。

    比例用百分数存，同一来源科室的比例之和应为 100；不强制校验为 100，
    因为分期建规则时中间态必然不足 100，改由分摊接口在计算时提示。
    """

    __tablename__ = "cost_allocation_rules"
    __table_args__ = (
        UniqueConstraint("from_dept_id", "to_dept_id", name="uq_alloc_from_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    from_dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    to_dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    ratio_pct: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChargePriceChange(Base):
    """收费项目调价历史（浙江省指南 #55 价格管理与公示）。

    价格是要对外公示的，而公示的前提是**改过什么、什么时候改的、谁改的**
    能说清楚。直接 UPDATE 覆盖 `charge_items.price` 会把这些全抹掉，
    到时候患者质疑"上个月不是这个价"，机构拿不出任何东西。

    已计费的明细不受调价影响——`bill_details` 存的是计费时的价格快照，
    所以这张表纯粹是**对外解释用**的账，不参与任何金额计算。
    """

    __tablename__ = "charge_price_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("charge_items.id"), index=True)
    old_price: Mapped[float] = mapped_column(Money)
    new_price: Mapped[float] = mapped_column(Money)
    # 调价依据（如"省医保局 2026 年第 3 号文"）。留空也允许——真实场景里
    # 补录历史价格时常常找不到原始文号，强制填写只会逼人编一个。
    reason: Mapped[str] = mapped_column(String(256), default="")
    effective_date: Mapped[str] = mapped_column(String(10), default="")
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FundPool(Base):
    """医保基金池：总额付费的账本主体（可不启用）。

    不建池子的县完全感觉不到这个模块存在——它不参与任何既有统计，
    也不改变结算流程。总额付费本身是地方选择，平台只提供账。

    `org_group_id` 可空：有的县按全域建一个池，有的按片区分池。绑到分组而不是
    机构，因为池子天然是"一群机构共用一笔钱"。
    """

    __tablename__ = "fund_pools"
    __table_args__ = (
        UniqueConstraint("year", "insurance_type", "org_group_id", name="uq_fund_pool_scope"),
        # D-2：上面那条唯一约束管不住全域池——SQL 里 NULL != NULL，`org_group_id`
        # 为空的行彼此不冲突，并发下实测建出过两个池，同一笔结余会被分两次。
        # 应用层查重是 check-then-act，中间有竞态窗口，兜底必须落在数据库上。
        # 部分唯一索引 SQLite 3.8+ 与 PostgreSQL 都支持；国产库（达梦/人大金仓）
        # 若不支持，需在阶段十二适配时改为"全域池写入哨兵值 0 而非 NULL"。
        Index(
            "uq_fund_pool_global",
            "year",
            "insurance_type",
            unique=True,
            sqlite_where=text("org_group_id IS NULL"),
            postgresql_where=text("org_group_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    # resident=城乡居民, employee=城镇职工
    insurance_type: Mapped[str] = mapped_column(String(16), default="resident", index=True)
    org_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_groups.id"), nullable=True, index=True
    )
    # 筹资总额（元）：年初核定的总盘子
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    prepay_ratio_pct: Mapped[float] = mapped_column(Money, default=0)
    # active=执行中, settled=已清算, closed=已归档
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundPrepayment(Base):
    """预付批次：年初/分批预拨给医共体的资金，**产生真实资金流**。"""

    __tablename__ = "fund_prepayments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    amount: Mapped[float] = mapped_column(Money)
    paid_date: Mapped[str] = mapped_column(String(10), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundPeriod(Base):
    """周期预结：按月归集实际发生额，与预付对冲看账面进度。

    **预结不产生资金流**，只是账面对冲。真正的钱在预付与年终清算两处流动。
    与预付、清算分表而不是混在一张"资金流水"里——年终对不上账时，
    必须能一眼分清"这笔是账面数还是真给了钱"。
    """

    __tablename__ = "fund_periods"
    __table_args__ = (UniqueConstraint("pool_id", "period", name="uq_fund_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    # 当期实际发生的医保支付额（由结算单归集，也允许人工调整后覆盖）
    actual_amount: Mapped[float] = mapped_column(Money, default=0)
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto=系统归集, manual=人工
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundSettlement(Base):
    """年终清算单：一个池子一次，结出结余或超支。

    `balance` 为负即超支。**平台不自动扣减**——超支怎么办是政策决定
    （分摊/挂账/不处理），这里只记录选择，不代替任何人做决定。
    """

    __tablename__ = "fund_settlements"
    __table_args__ = (UniqueConstraint("pool_id", name="uq_fund_settlement_pool"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), unique=True, index=True)
    total_income: Mapped[float] = mapped_column(Money, default=0)   # 筹资总额快照
    total_expense: Mapped[float] = mapped_column(Money, default=0)  # 全年实际发生额
    balance: Mapped[float] = mapped_column(Money, default=0)        # 正=结余，负=超支
    # none=不处理（默认）, share=按分配公式分摊, carry=挂账结转
    overrun_action: Mapped[str] = mapped_column(String(16), default="none")
    # 分配公式（AST 白名单求值），返回**份额权重**，由平台归一化后乘结余额。
    # 各县办法不同且逐年调整，绝不写死进代码。
    formula_expr: Mapped[str] = mapped_column(String(256), default="score")
    # 绩效得分快照取自哪一次考核（记录参数，便于复现）
    score_basis: Mapped[str] = mapped_column(String(128), default="")
    settled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FundDistribution(Base):
    """结余分配明细：清算后按机构分钱。

    `score` 与 `score_detail` 是**冻结的快照**。绩效指标权重随时可调，
    若分钱时"当下重算"，等于分完钱还能改分——与知情告知书冻结正文同理。
    """

    __tablename__ = "fund_distributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("fund_settlements.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=0)      # 公式求出的份额权重
    share_pct: Mapped[float] = mapped_column(Float, default=0)   # 归一化后占比
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
