"""会计核算（T3.1）：科目体系与记账凭证（复式记账）。

此前 `/api/mgmt/finance` 的 FinanceEntry 只是"期间 + 收/支 + 金额"的流水台账，
出不了资产负债表，也无从核对借贷是否平衡。这里补上科目与凭证：

- **借贷必平**：保存时强校验 Σ借 = Σ贷，且单条分录只能落一边；
- **过账即锁定**：posted 后不可改，冲销走作废（void）而不是删除——账务留痕
  的意义就在于错了也要看得见；
- 与 FinanceEntry 并存：后者是业务口径的收支汇总，不被取代。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, resolve_org_scope
from ..models import AccountSubject, Organization, User, Voucher, VoucherEntry, utcnow

router = APIRouter(prefix="/api/accounting", tags=["会计核算"], dependencies=[Depends(get_current_user)])

# 借贷平衡的容差：金额用 float 存，两位小数以内的浮点误差不算不平
BALANCE_TOLERANCE = 0.005


# ---------------------------------------------------------------- 科目


class SubjectIn(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(pattern="^(asset|liability|net_asset|income|expense)$")
    direction: str = Field(default="debit", pattern="^(debit|credit)$")


@router.post("/subjects", status_code=201, dependencies=[Depends(require_admin)])
def create_subject(body: SubjectIn, db: Session = Depends(get_db)):
    subject = AccountSubject(**body.model_dump())
    db.add(subject)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="科目编码已存在") from None
    db.refresh(subject)
    return _subject_out(subject)


def _subject_out(s: AccountSubject) -> dict:
    return {
        "id": s.id,
        "code": s.code,
        "name": s.name,
        "category": s.category,
        "direction": s.direction,
        "active": s.active,
    }


@router.get("/subjects")
def list_subjects(category: str | None = None, db: Session = Depends(get_db)):
    query = db.query(AccountSubject).filter(AccountSubject.active.is_(True))
    if category:
        query = query.filter(AccountSubject.category == category)
    return [_subject_out(s) for s in query.order_by(AccountSubject.code).all()]


# ---------------------------------------------------------------- 凭证


class EntryIn(BaseModel):
    subject_code: str = Field(min_length=1, max_length=16)
    summary: str = ""
    debit: float = Field(default=0, ge=0)
    credit: float = Field(default=0, ge=0)


class VoucherIn(BaseModel):
    org_id: int
    voucher_no: str = Field(min_length=1, max_length=32)
    voucher_date: str = Field(min_length=10, max_length=10)
    period: str = Field(default="", max_length=7)
    summary: str = ""
    entries: list[EntryIn] = Field(min_length=2)


def _validate_entries(db: Session, entries: list[EntryIn]) -> tuple[float, float]:
    """校验分录：科目存在、单边填列、借贷平衡。返回 (借方合计, 贷方合计)。"""
    codes = {e.subject_code for e in entries}
    known = {
        c for (c,) in db.query(AccountSubject.code).filter(AccountSubject.code.in_(codes)).all()
    }
    missing = codes - known
    if missing:
        raise HTTPException(status_code=422, detail=f"科目不存在：{'、'.join(sorted(missing))}")
    for entry in entries:
        if (entry.debit > 0) == (entry.credit > 0):
            raise HTTPException(
                status_code=422,
                detail=f"分录 {entry.subject_code} 必须且只能填列借方或贷方之一",
            )
    total_debit = round(sum(e.debit for e in entries), 2)
    total_credit = round(sum(e.credit for e in entries), 2)
    if abs(total_debit - total_credit) > BALANCE_TOLERANCE:
        raise HTTPException(
            status_code=422, detail=f"借贷不平：借方 {total_debit}，贷方 {total_credit}"
        )
    return total_debit, total_credit


def _voucher_out(v: Voucher, entries: list[VoucherEntry] | None = None) -> dict:
    out = {
        "id": v.id,
        "org_id": v.org_id,
        "period": v.period,
        "voucher_no": v.voucher_no,
        "voucher_date": v.voucher_date,
        "summary": v.summary,
        "total_debit": v.total_debit,
        "total_credit": v.total_credit,
        "status": v.status,
    }
    if entries is not None:
        out["entries"] = [
            {"subject_code": e.subject_code, "summary": e.summary, "debit": e.debit, "credit": e.credit}
            for e in entries
        ]
    return out


@router.post("/vouchers", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_voucher(
    body: VoucherIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """录入凭证（草稿）。借贷不平直接 422，不给"先存下来以后再平"的口子。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    total_debit, total_credit = _validate_entries(db, body.entries)
    voucher = Voucher(
        org_id=body.org_id,
        period=body.period or body.voucher_date[:7],
        voucher_no=body.voucher_no,
        voucher_date=body.voucher_date,
        summary=body.summary,
        total_debit=total_debit,
        total_credit=total_credit,
        created_by=user.id,
    )
    db.add(voucher)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该机构下凭证号已存在") from None
    entries = [
        VoucherEntry(
            voucher_id=voucher.id,
            subject_code=e.subject_code,
            summary=e.summary,
            debit=e.debit,
            credit=e.credit,
        )
        for e in body.entries
    ]
    db.add_all(entries)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该机构下凭证号已存在") from None
    db.refresh(voucher)
    return _voucher_out(voucher, entries)


@router.get("/vouchers")
def list_vouchers(
    response: Response,
    org_id: int | None = None,
    period: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(Voucher)
    if org_id is not None:
        query = query.filter(Voucher.org_id == org_id)
    if period:
        query = query.filter(Voucher.period == period)
    if status:
        query = query.filter(Voucher.status == status)
    return [
        _voucher_out(v) for v in paginate(query.order_by(Voucher.id.desc()), response, offset, limit)
    ]


@router.get("/vouchers/{voucher_id}")
def get_voucher(voucher_id: int, db: Session = Depends(get_db)):
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    entries = db.query(VoucherEntry).filter(VoucherEntry.voucher_id == voucher_id).all()
    return _voucher_out(voucher, entries)


@router.post("/vouchers/{voucher_id}/post", dependencies=[Depends(require_roles("director"))])
def post_voucher(voucher_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """过账：草稿 → 已过账，之后不可修改。"""
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if voucher.status != "draft":
        raise HTTPException(status_code=409, detail=f"当前状态 {voucher.status} 不可过账")
    voucher.status = "posted"
    voucher.posted_by = user.id
    voucher.posted_at = utcnow()
    db.commit()
    return {"id": voucher.id, "status": voucher.status}


@router.post("/vouchers/{voucher_id}/void", dependencies=[Depends(require_roles("director"))])
def void_voucher(voucher_id: int, db: Session = Depends(get_db)):
    """作废：已过账凭证的更正手段。不提供删除——账错了也要看得见。"""
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if voucher.status == "void":
        raise HTTPException(status_code=409, detail="凭证已作废")
    voucher.status = "void"
    db.commit()
    return {"id": voucher.id, "status": voucher.status}


@router.get("/trial-balance")
def trial_balance(period: str, org_id: int | None = None, db: Session = Depends(get_db)):
    """试算平衡表：按科目汇总已过账凭证的借贷发生额。

    只统计 posted——草稿未生效、作废已撤销，计进去就不是账了。
    """
    query = (
        db.query(
            VoucherEntry.subject_code,
            func.sum(VoucherEntry.debit).label("debit"),
            func.sum(VoucherEntry.credit).label("credit"),
        )
        .join(Voucher, VoucherEntry.voucher_id == Voucher.id)
        .filter(Voucher.period == period, Voucher.status == "posted")
    )
    if org_id is not None:
        query = query.filter(Voucher.org_id == org_id)
    rows = query.group_by(VoucherEntry.subject_code).all()
    subjects = {
        s.code: s for s in db.query(AccountSubject).all()
    }
    lines = []
    for code, debit, credit in rows:
        subject = subjects.get(code)
        lines.append(
            {
                "subject_code": code,
                "subject_name": subject.name if subject else "",
                "category": subject.category if subject else "",
                "debit": round(debit or 0, 2),
                "credit": round(credit or 0, 2),
            }
        )
    total_debit = round(sum(x["debit"] for x in lines), 2)
    total_credit = round(sum(x["credit"] for x in lines), 2)
    return {
        "period": period,
        "lines": sorted(lines, key=lambda x: x["subject_code"]),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": abs(total_debit - total_credit) <= BALANCE_TOLERANCE,
    }


# ---------------------------------------------------------------- 合并报表


# 报表方向：资产与费用是借方增加，负债/净资产/收入是贷方增加。
# 余额 = 增加方 − 减少方，故两类的算式互为相反，不能共用一套。
_DEBIT_SIDE = {"asset", "expense"}

CATEGORY_NAMES = {
    "asset": "资产",
    "liability": "负债",
    "net_asset": "净资产",
    "income": "收入",
    "expense": "费用",
}


def _balances(db: Session, period: str, org_ids: list[int] | None):
    """按机构 × 科目算已过账凭证的期间余额。

    只统计 posted——草稿未生效、作废已撤销，与试算平衡表同一口径。
    """
    query = (
        db.query(
            Voucher.org_id,
            VoucherEntry.subject_code,
            func.sum(VoucherEntry.debit).label("debit"),
            func.sum(VoucherEntry.credit).label("credit"),
        )
        .join(Voucher, VoucherEntry.voucher_id == Voucher.id)
        .filter(Voucher.period == period, Voucher.status == "posted")
    )
    if org_ids is not None:
        query = query.filter(Voucher.org_id.in_(org_ids))
    return query.group_by(Voucher.org_id, VoucherEntry.subject_code).all()


@router.get("/consolidated-statements")
def consolidated_statements(
    period: str,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """合并报表：资产负债表与收入费用表口径的多院合并（指引㉛"财务报表叠加汇总"）。

    与既有两个接口的分工，写清楚免得三者被混用：

    - `/api/mgmt/finance/summary` 汇总的是 **收支流水**（`finance_entries`），
      口径粗、录入快，给的是"这个月收了多少花了多少"；
    - `/api/accounting/trial-balance` 是 **试算平衡表**，按科目列借贷发生额，
      用来查账平不平；
    - 这里是 **报表口径的合并**，按科目类别归到资产/负债/净资产/收入/费用五张
      口袋里，并按机构分列 + 合并列——医共体"独立建账、集中核算"要的正是
      "每家多少、合起来多少"同时看得见。

    三条口径：

    1. **合并是各院简单相加，不做内部往来抵销**。医共体成员单位之间确有往来
       （统一采购、集中支付），严格的合并报表要抵销内部交易。平台目前**没有
       内部交易标记**，做不了抵销——所以这里如实叫"叠加汇总"，
       并在返回里标明 `elimination: false`，不冒充合并会计报表。
    2. **余额按科目方向算**：资产/费用用借−贷，负债/净资产/收入用贷−借。
       统一用一个方向算，负债会变成负数，报表直接不能看。
    3. **不平的期间照样出表，但标出来**。出不了表的时候恰恰最需要看表——
       把差额报出来比拒绝返回有用。
    """
    scope = resolve_org_scope(db, group_id, org_id)
    rows = _balances(db, period, scope)
    subjects = {s.code: s for s in db.query(AccountSubject).all()}

    org_ids = sorted({r.org_id for r in rows})
    names = {
        o.id: o.name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}

    def blank() -> dict:
        return {k: 0.0 for k in CATEGORY_NAMES}

    by_org: dict[int, dict] = {}
    consolidated = blank()
    unknown_subjects: set[str] = set()
    total_debit = total_credit = 0.0

    for org, code, debit, credit in rows:
        debit, credit = float(debit or 0), float(credit or 0)
        total_debit += debit
        total_credit += credit
        subject = subjects.get(code)
        if subject is None:
            # 科目表里没有的编码：不静默丢弃，也不硬塞进某一类
            unknown_subjects.add(code)
            continue
        amount = (debit - credit) if subject.category in _DEBIT_SIDE else (credit - debit)
        entry = by_org.setdefault(org, blank())
        entry[subject.category] += amount
        consolidated[subject.category] += amount

    def statement(bucket: dict) -> dict:
        assets = round(bucket["asset"], 2)
        liabilities = round(bucket["liability"], 2)
        net_assets = round(bucket["net_asset"], 2)
        income = round(bucket["income"], 2)
        expense = round(bucket["expense"], 2)
        surplus = round(income - expense, 2)
        return {
            "balance_sheet": {
                "assets": assets,
                "liabilities": liabilities,
                "net_assets": net_assets,
                # 期间结余尚未结转净资产，故恒等式要把它算进右边，
                # 否则每个未结转的期间看起来都不平
                "check": round(assets - (liabilities + net_assets + surplus), 2),
            },
            "income_statement": {
                "income": income,
                "expense": expense,
                "surplus": surplus,
            },
        }

    return {
        "period": period,
        "group_id": group_id,
        "orgs": [
            {"org_id": oid, "org_name": names.get(oid, ""), **statement(bucket)}
            for oid, bucket in sorted(by_org.items())
        ],
        "consolidated": statement(consolidated),
        "voucher_totals": {
            "debit": round(total_debit, 2),
            "credit": round(total_credit, 2),
            # 不平也出表，把差额标出来——出不了表的时候恰恰最需要看表
            "balanced": abs(total_debit - total_credit) <= BALANCE_TOLERANCE,
            "difference": round(total_debit - total_credit, 2),
        },
        # 科目表里查不到的编码单列，不静默丢弃也不硬塞进某一类
        "unknown_subject_codes": sorted(unknown_subjects),
        "caliber": {
            "elimination": False,
            "note": "合并为各院简单相加，**未做内部往来抵销**——平台目前没有内部"
                    "交易标记，做不了抵销，故如实称叠加汇总而非合并会计报表",
            "direction": "资产/费用按借−贷，负债/净资产/收入按贷−借",
            "check": "资产 −（负债 + 净资产 + 本期结余）；期间结余未结转净资产，"
                     "故恒等式把结余算在右边",
        },
    }
