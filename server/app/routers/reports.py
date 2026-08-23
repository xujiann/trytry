"""上报报表（块5）：监测指标体系当期值 JSON 与 CSV 导出、运营月报 CSV。

- 指标口径对齐《紧密型县域医共体监测指标体系（2024版）》平台可计算的 14 项
- 全部接口限管理层（director）/管理员（admin 守卫内始终放行）
- CSV 带 UTF-8 BOM（Excel 直接打开不乱码），列含 指标名/口径/当期值/数据来源
"""
import csv
import io
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..clock import now_aware
from ..database import get_db
from ..deps import require_roles
from ..models import (
    Admission,
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    FamilyDoctorContract,
    FinanceEntry,
    InsuranceSettlement,
    Organization,
    Patient,
    Prescription,
    Referral,
    Settlement,
)
from .performance import org_scorecards

router = APIRouter(
    prefix="/api/reports",
    tags=["上报报表"],
    dependencies=[Depends(require_roles("director"))],  # 报表上报=管理层（admin 放行）
)


def _pct(part: float, total: float) -> float:
    return round(part * 100.0 / total, 2) if total else 0.0


def _monitoring_indicators(db: Session) -> list[dict]:
    """监测指标体系 14 项当期值（平台内可自动计算口径）。"""
    org_total = db.query(func.count(Organization.id)).scalar() or 0
    patient_total = db.query(func.count(Patient.id)).scalar() or 0
    encounter_total = db.query(func.count(Encounter.id)).scalar() or 0
    grassroots_encounters = (
        db.query(func.count(Encounter.id))
        .join(Organization, Encounter.org_id == Organization.id)
        .filter(Organization.level.in_(["township", "village"]))
        .scalar()
        or 0
    )
    reported = (
        db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "reported").scalar() or 0
    )
    recognized = (
        db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "recognized").scalar()
        or 0
    )
    critical_open = (
        db.query(func.count(ExamReport.id))
        .filter(
            ExamReport.critical.is_(True),
            ExamReport.critical_status.in_(["notified", "acknowledged", ""]),
        )
        .scalar()
        or 0
    )
    ref_up = db.query(func.count(Referral.id)).filter(Referral.direction == "up").scalar() or 0
    ref_down = db.query(func.count(Referral.id)).filter(Referral.direction == "down").scalar() or 0
    ref_total = db.query(func.count(Referral.id)).scalar() or 0
    ref_completed = (
        db.query(func.count(Referral.id)).filter(Referral.status == "completed").scalar() or 0
    )
    rx_total = db.query(func.count(Prescription.id)).scalar() or 0
    rx_auto = (
        db.query(func.count(Prescription.id))
        .filter(Prescription.status == "auto_passed")
        .scalar()
        or 0
    )
    chronic_total = db.query(func.count(ChronicPatient.id)).scalar() or 0
    contracts_active = (
        db.query(func.count(FamilyDoctorContract.id))
        .filter(FamilyDoctorContract.status == "active")
        .scalar()
        or 0
    )
    ins_total = db.query(func.coalesce(func.sum(InsuranceSettlement.insurance_pay), 0.0)).scalar()
    ins_local = (
        db.query(func.coalesce(func.sum(InsuranceSettlement.insurance_pay), 0.0))
        .filter(InsuranceSettlement.settle_type == "local")
        .scalar()
    )
    inpatient_bills = (
        db.query(func.count(Settlement.id), func.coalesce(func.sum(Settlement.total_amount), 0.0))
        .filter(Settlement.bill_type == "inpatient")
        .one()
    )
    inpatient_avg = round(inpatient_bills[1] / inpatient_bills[0], 2) if inpatient_bills[0] else 0.0

    return [
        {"no": 1, "name": "医共体成员单位数", "caliber": "平台内注册的县乡村成员机构总数", "value": org_total, "unit": "家", "source": "机构管理"},
        {"no": 2, "name": "电子健康档案建档人数", "caliber": "EMPI 主索引患者档案总数（身份证号去重）", "value": patient_total, "unit": "人", "source": "患者主索引"},
        {"no": 3, "name": "县域内基层诊疗人次占比", "caliber": "乡/村两级机构就诊人次 ÷ 全部就诊人次 ×100%", "value": _pct(grassroots_encounters, encounter_total), "unit": "%", "source": "就诊记录"},
        {"no": 4, "name": "远程诊断服务量", "caliber": "共享诊断中心已出报告 + 已互认申请单数", "value": reported + recognized, "unit": "次", "source": "共享诊断中心"},
        {"no": 5, "name": "检查检验结果互认率", "caliber": "互认次数 ÷（已报告 + 互认）×100%", "value": _pct(recognized, reported + recognized), "unit": "%", "source": "互认目录与统计"},
        {"no": 6, "name": "危急值未闭环例数", "caliber": "危急值报告中未完成处置反馈（通知/确认中）例数", "value": critical_open, "unit": "例", "source": "危急值管理"},
        {"no": 7, "name": "上转人次", "caliber": "基层向上级机构转诊申请数", "value": ref_up, "unit": "人次", "source": "双向转诊"},
        {"no": 8, "name": "下转人次", "caliber": "上级向基层机构转诊申请数", "value": ref_down, "unit": "人次", "source": "双向转诊"},
        {"no": 9, "name": "转诊结案率", "caliber": "已结案转诊 ÷ 全部转诊 ×100%", "value": _pct(ref_completed, ref_total), "unit": "%", "source": "双向转诊"},
        {"no": 10, "name": "处方系统审核通过率", "caliber": "系统前置审核直接通过处方 ÷ 全部处方 ×100%", "value": _pct(rx_auto, rx_total), "unit": "%", "source": "集中审方"},
        {"no": 11, "name": "慢病在管人数", "caliber": "高血压/糖尿病/慢阻肺等慢病在管档案数", "value": chronic_total, "unit": "人", "source": "慢病管理"},
        {"no": 12, "name": "家庭医生签约人数", "caliber": "履约中家庭医生签约协议数", "value": contracts_active, "unit": "人", "source": "家医签约"},
        {"no": 13, "name": "县域内医保基金支出占比", "caliber": "本地结算医保支付 ÷ 全部医保支付 ×100%", "value": _pct(ins_local, ins_total), "unit": "%", "source": "医保结算"},
        {"no": 14, "name": "住院均次费用", "caliber": "住院结算总额 ÷ 住院结算笔数", "value": inpatient_avg, "unit": "元", "source": "费用结算"},
    ]


def _money(value: float) -> str:
    """金额导出统一格式：定点两位小数。"""
    return f"{float(value):.2f}"


def _csv_response(filename: str, header: list[str], rows: list[list]) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    payload = "\ufeff" + buffer.getvalue()  # BOM：Excel 直接打开不乱码
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/monitoring")
def monitoring_report(db: Session = Depends(get_db)):
    """监测指标体系 14 项指标当期值（JSON，供上报与驾驶舱复核）。"""
    indicators = _monitoring_indicators(db)
    return {
        "generated_at": now_aware().isoformat(),
        "total": len(indicators),
        "indicators": indicators,
    }


@router.get("/monitoring/export")
def export_monitoring_csv(db: Session = Depends(get_db)):
    """监测指标 CSV 下载：序号/指标名/口径/当期值/单位/数据来源。"""
    rows = [
        [i["no"], i["name"], i["caliber"], i["value"], i["unit"], i["source"]]
        for i in _monitoring_indicators(db)
    ]
    return _csv_response(
        "monitoring_indicators.csv", ["序号", "指标名", "口径", "当期值", "单位", "数据来源"], rows
    )


@router.get("/operations/export")
def export_operations_csv(period: str | None = None, db: Session = Depends(get_db)):
    """运营月报 CSV：各机构 就诊/住院/收入/支出/结余/绩效分。

    period=YYYY-MM 时就诊/住院按发生月份、收支按记账期间过滤；缺省为累计口径。

    **例外：绩效分列没有"累计"**——绩效评分自 2026-08 起是周期口径
    （见 docs/统计口径对照表.md 第 3 条），缺省即当年。表头已注明分数所属周期。
    """
    if period is not None and not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=422, detail="period 格式须为 YYYY-MM")
    orgs = db.query(Organization).order_by(Organization.id).all()
    # 绩效分跟着报表周期走：其余各列都按 period 过滤，分数列不跟就会出现
    # "本月就诊 30 人次、绩效分却是全年累计"这种一行里两个口径的报表。
    # （绩效评分自 2026-08 起是周期口径，`period=None` 即当年，
    #  故"累计"导出的分数列实为当年——CSV 表头已注明。）
    scorecard_payload = org_scorecards(period=period, db=db)
    score_period = scorecard_payload["period"]
    scores = {c["org_id"]: c["score"] for c in scorecard_payload["scorecards"]}
    level_names = {"county": "县级", "township": "乡级", "village": "村级", "city": "市级"}

    def month_of(dt) -> str:
        return dt.strftime("%Y-%m") if dt else ""

    encounters = db.query(Encounter.org_id, Encounter.created_at).all()
    admissions = db.query(Admission.org_id, Admission.admitted_at).all()
    finance_query = db.query(
        FinanceEntry.org_id, FinanceEntry.category, func.coalesce(func.sum(FinanceEntry.amount), 0.0)
    )
    if period:
        finance_query = finance_query.filter(FinanceEntry.period == period)
    finance: dict[int, dict[str, float]] = {}
    for org_id, category, amount in finance_query.group_by(
        FinanceEntry.org_id, FinanceEntry.category
    ).all():
        finance.setdefault(org_id, {})[category] = round(amount, 2)

    rows = []
    for org in orgs:
        enc_count = sum(
            1
            for org_id, at in encounters
            if org_id == org.id and (period is None or month_of(at) == period)
        )
        adm_count = sum(
            1
            for org_id, at in admissions
            if org_id == org.id and (period is None or month_of(at) == period)
        )
        income = float(finance.get(org.id, {}).get("income", 0.0) or 0)
        expense = float(finance.get(org.id, {}).get("expense", 0.0) or 0)
        rows.append(
            [
                org.id,
                org.name,
                level_names.get(org.level, org.level),
                enc_count,
                adm_count,
                # 金额一律格式化到分再导出。阶段十二把金额列改成 NUMERIC 之后，
                # 整数金额从库里读回来是 int（80000 而不是 80000.0），
                # 直接 str() 会让同一列时而带小数点时而不带——导出的表格给人看，
                # 也常被 Excel 再加工，列格式不稳定比少两位小数麻烦得多。
                _money(income),
                _money(expense),
                _money(income - expense),
                scores.get(org.id, 0.0),
            ]
        )
    suffix = period or "all"
    return _csv_response(
        f"operations_report_{suffix}.csv",
        # 绩效评分列注明所属周期：它是周期口径，与同一行"累计"的其它列不同源，
        # 表头不标清楚，拿去做二次加工的人会当成同一口径。
        ["机构ID", "机构名称", "层级", "门急诊人次", "住院人次", "收入(元)", "支出(元)",
         "结余(元)", f"绩效评分({score_period})"],
        rows,
    )
