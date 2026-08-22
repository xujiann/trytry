"""报告打印（指引①②③④"报告打印"）：服务端渲染 A4 版式可打印 HTML。

- 覆盖单据：检查报告、处方笺、检查/检验申请单、法定医学证明，以及 B2 补齐的
  住院费用清单、结算单、病案首页、体检报告（含分项）、知情同意书、
  疫苗接种证明、转诊单、出院小结
- 患者敏感字段脱敏规则与业务接口完全一致（复用 privacy 模块，非 admin 掩码）
- 版式：@page A4 + @media print，打印时隐藏操作按钮，页脚带打印时间
- 模板可配：PrintTemplate（doc_type 唯一）配置抬头机构名、页脚说明与二维码开关
- 契约：单据端点响应即 text/html 字符串（response_model=str 只为声明契约，
  渲染直接返回 HTMLResponse，字节不经序列化）

**B2 新增单据的字段口径（不虚构模型没有的字段）**：
- 住院费用清单：按 admission 汇总 BillDetail 明细（价格快照口径）；
- 住院/门诊结算单：Settlement 一行（总额/医保/自付），关联住院或就诊；
- 病案首页：CaseSummary 最小数据集（出院诊断/手术/费用/转归/DRG），无
  完整国标首页字段——扩充属数据模型任务，不在打印层伪造；
- 体检报告：PhysicalExam 汇总 + CheckupItem 分项 + 总检结论/总检医师；
- 知情同意书：ConsentRecord + 其引用版本的 ConsentText 文本（版本对不上时
  退到该场景当前 active 版并在文中标注版本号，举证以记录里的版本号为准）；
- 疫苗接种证明：按单条 VaccinationRecord 出证（剂次/批号/接种单位/接种者）；
- 转诊单：Referral（转出/转入机构、方向、事由、状态）；
- 出院小结：Admission + CaseSummary + 出院病程记录（ProgressNote
  note_type="discharge"，未书写则该栏留"—"），仅限已出院者。

**可见性（P0 整改）**：四个单据打印端点此前只要"登录"就渲染——脱敏做了，
但脱敏挡不住"这个人根本不该看这张单子"。任何账号按 id 顺序遍历，就能把全县的
报告单、处方笺、申请单打出来，且不留任何痕迹，正是 CLAUDE.md §8 明令禁止的
"按 id 直取、不校验归属、无留痕"。现在一律先过 `assert_patient_visible`
（校验 + 写 AccessLog）；医学证明未关联患者时退到签发机构的可见性。
"""
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_local
from ..concurrency import upsert_unique
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import (
    Admission,
    BillDetail,
    CaseSummary,
    CheckupItem,
    ConsentRecord,
    ConsentText,
    ExamReport,
    ExamRequest,
    MedicalCert,
    Organization,
    Patient,
    PhysicalExam,
    Prescription,
    PrescriptionItem,
    PrintTemplate,
    ProgressNote,
    Referral,
    Settlement,
    User,
    VaccinationRecord,
)
from ..privacy import mask_id_card, mask_phone
from ..visibility import assert_org_visible, assert_patient_visible

router = APIRouter(prefix="/api/print", tags=["报告打印"], dependencies=[Depends(get_current_user)])

DOC_TYPES = {
    "exam_report": "检查检验报告单",
    "prescription": "处方笺",
    "exam_request": "检查检验申请单",
    "cert": "医学证明",
    "inpatient_bill": "住院费用清单",
    "settlement": "结算单",
    "case_summary": "病案首页",
    "checkup_report": "体检报告",
    "consent": "知情同意书",
    "vaccine_cert": "疫苗接种证明",
    "referral": "转诊单",
    "discharge_summary": "出院小结",
}

#: 模板 doc_type 校验：由 DOC_TYPES 生成，加新单据不必再改第二处
_DOC_TYPE_PATTERN = "^(" + "|".join(DOC_TYPES) + ")$"

CENTER_NAMES = {"imaging": "影像", "ecg": "心电", "lab": "检验", "pathology": "病理"}
CERT_TYPE_NAMES = {"birth": "出生医学证明", "death": "死亡医学证明", "defect": "出生缺陷儿登记"}
RX_STATUS_NAMES = {
    "auto_passed": "系统审通过",
    "pending_review": "待药师审核",
    "approved": "药师审核通过",
    "rejected": "已退回",
}
EXAM_STATUS_NAMES = {
    "pending": "待诊断",
    "diagnosing": "诊断中",
    "reported": "已报告",
    "recognized": "结果互认",
}
BILL_TYPE_NAMES = {"outpatient": "门诊", "inpatient": "住院"}
REFERRAL_DIRECTION_NAMES = {"up": "上转", "down": "下转"}
# 转诊状态文案与业务端逐字相同，不再抄一份：打印件与列表页读起来必须是同一句话。
# 居民端另有一套措辞（待接收/已接收/已完成，见 portal._PLATFORM_REFERRAL_STATUS），
# 那是刻意的对外分叉、不是第三份拷贝，收敛与否属另案（ROADMAP）。
from .referrals import STATUS_LABELS as REFERRAL_STATUS_NAMES  # noqa: E402
CONSENT_SCENE_NAMES = {
    "archive": "居民健康建档",
    "chronic_enroll": "慢病入组管理",
    "followup": "随访服务",
    "family_contract": "家庭医生签约",
    "cross_org_access": "跨机构调阅",
    "public_health_report": "公共卫生上报",
    "family_delegate": "家庭代管授权",
}
CONSENT_METHOD_NAMES = {"self": "居民端本人自签", "proxy": "窗口代录"}

DEFAULT_FOOTER = "本单据由县域医共体信息化平台生成，打印件仅供参考，以电子病历记录为准。"

# A4 版式样式：屏幕预览与打印一致，打印时去掉页面阴影与操作区
_PAGE_CSS = """
@page { size: A4; margin: 15mm 14mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #eceff1; font-family: "Microsoft YaHei", "PingFang SC", SimSun, sans-serif;
       color: #1b1f23; font-size: 13px; line-height: 1.7; }
.sheet { width: 210mm; min-height: 297mm; margin: 12px auto; padding: 15mm 14mm; background: #fff;
         box-shadow: 0 2px 12px rgba(0,0,0,.18); }
.doc-header { text-align: center; border-bottom: 2px solid #1b1f23; padding-bottom: 8px; }
.doc-header .org { font-size: 20px; font-weight: 700; letter-spacing: 2px; }
.doc-header .doc-title { font-size: 16px; margin-top: 4px; letter-spacing: 6px; }
.doc-no { text-align: right; font-size: 12px; color: #444; margin-top: 4px; }
.meta { width: 100%; border-collapse: collapse; margin-top: 10px; }
.meta td { padding: 4px 6px; border-bottom: 1px dashed #b8c0c8; }
.meta td.k { color: #555; width: 84px; }
table.items { width: 100%; border-collapse: collapse; margin-top: 10px; }
table.items th, table.items td { border: 1px solid #666; padding: 5px 6px; font-size: 12.5px; }
table.items th { background: #f2f5f7; }
.section { margin-top: 12px; }
.section h3 { font-size: 13.5px; margin: 0 0 4px; padding-left: 6px; border-left: 3px solid #1b1f23; }
.section .body { min-height: 40px; white-space: pre-wrap; border: 1px solid #ccc; padding: 6px 8px; }
.critical { color: #b00020; font-weight: 700; border: 1px solid #b00020; padding: 2px 8px; }
.sign { margin-top: 18px; display: flex; justify-content: space-between; font-size: 12.5px; }
.qr { margin-top: 14px; }
.qr .box { width: 76px; height: 76px; border: 1px solid #666; display: flex; align-items: center;
           justify-content: center; font-size: 11px; color: #666; text-align: center; }
.doc-footer { margin-top: 16px; border-top: 1px solid #999; padding-top: 6px; font-size: 11.5px; color: #555;
              display: flex; justify-content: space-between; }
.toolbar { text-align: center; margin: 10px; }
.toolbar button { padding: 6px 18px; font-size: 14px; cursor: pointer; }
@media print {
  body { background: #fff; }
  .sheet { width: auto; min-height: 0; margin: 0; padding: 0; box-shadow: none; }
  .toolbar { display: none; }
}
"""


def _esc(value) -> str:
    return escape(str(value if value is not None else ""))


def _template(db: Session, doc_type: str) -> PrintTemplate | None:
    return db.query(PrintTemplate).filter(PrintTemplate.doc_type == doc_type).first()


def _org_name(db: Session, org_id: int | None) -> str:
    if org_id is None:
        return ""
    org = db.get(Organization, org_id)
    return org.name if org else ""


def _user_name(db: Session, user_id: int | None) -> str:
    if user_id is None:
        return ""
    user = db.get(User, user_id)
    if user is None:
        return ""
    return user.full_name or user.username


def _patient_rows(patient: Patient | None, viewer: User) -> str:
    """患者信息区：脱敏规则与业务接口一致（非 admin 掩码身份证与电话）。"""
    if patient is None:
        return '<tr><td class="k">患者</td><td colspan="3">—</td></tr>'
    id_card, phone = patient.id_card, patient.phone
    if viewer.role != "admin":
        id_card = mask_id_card(id_card)
        phone = mask_phone(phone)
    return (
        f'<tr><td class="k">姓名</td><td>{_esc(patient.name)}</td>'
        f'<td class="k">性别</td><td>{_esc(patient.gender)}</td></tr>'
        f'<tr><td class="k">出生日期</td><td>{_esc(patient.birth_date) or "—"}</td>'
        f'<td class="k">健康卡号</td><td>{_esc(patient.ehc_no)}</td></tr>'
        f'<tr><td class="k">身份证号</td><td>{_esc(id_card)}</td>'
        f'<td class="k">联系电话</td><td>{_esc(phone) or "—"}</td></tr>'
    )


def _render(
    *,
    doc_type: str,
    template: PrintTemplate | None,
    org_name: str,
    doc_title: str,
    doc_no: str,
    meta_rows: str,
    body_html: str,
) -> HTMLResponse:
    header_org = (template.header_org_name if template and template.header_org_name else org_name) or "县域医共体"
    footer = (template.footer_note if template and template.footer_note else DEFAULT_FOOTER)
    show_qr = True if template is None else bool(template.show_qr)
    qr_html = (
        '<div class="qr"><div class="box">二维码<br>（验真占位）</div></div>' if show_qr else ""
    )
    printed_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(doc_title)}</title>
<style>{_PAGE_CSS}</style></head>
<body data-doc-type="{_esc(doc_type)}">
<div class="toolbar"><button onclick="window.print()">打印本页</button></div>
<div class="sheet">
  <div class="doc-header"><div class="org">{_esc(header_org)}</div>
    <div class="doc-title">{_esc(doc_title)}</div></div>
  <div class="doc-no">单据编号：{_esc(doc_no)}</div>
  <table class="meta">{meta_rows}</table>
  {body_html}
  {qr_html}
  <div class="doc-footer"><span>{_esc(footer)}</span><span>打印时间：{_esc(printed_at)}</span></div>
</div>
</body></html>"""
    return HTMLResponse(content=html)


# ---------- 检查报告打印 ----------


@router.get("/exam-reports/{report_id}", response_class=HTMLResponse, response_model=str)
def print_exam_report(
    report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """检查检验报告单打印版：机构抬头、患者信息、项目、所见、结论、危急值标记与报告医师。"""
    report = db.get(ExamReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    request = db.get(ExamRequest, report.request_id)
    if request is None:
        # 报告失去申请单就无从判定归属——宁可拒绝，也不退化成无校验
        raise HTTPException(status_code=404, detail="报告对应的申请单不存在")
    assert_patient_visible(db, user, request.patient_id, resource="print:exam_report")
    patient = db.get(Patient, request.patient_id)
    org_name = _org_name(db, request.from_org_id)
    center = CENTER_NAMES.get(request.center_type, request.center_type) if request else ""
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">申请机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">检查类别</td><td>{_esc(center)}</td></tr>'
        f'<tr><td class="k">检查项目</td><td>{_esc(request.item_name if request else "")}</td>'
        f'<td class="k">项目编码</td><td>{_esc(request.item_code if request else "")}</td></tr>'
        f'<tr><td class="k">临床资料</td><td colspan="3">{_esc(request.clinical_info if request else "") or "—"}</td></tr>'
    )
    critical_html = (
        '<p><span class="critical">危急值 ★ 已按危急值流程通知临床</span></p>'
        if report.critical
        else ""
    )
    body = f"""
  <div class="section"><h3>检查所见</h3><div class="body">{_esc(report.finding) or "—"}</div></div>
  <div class="section"><h3>诊断结论</h3><div class="body">{_esc(report.conclusion) or "—"}</div></div>
  {critical_html}
  <div class="sign"><span>报告医师：{_esc(report.reported_by) or "—"}</span>
    <span>报告时间：{_esc(report.reported_at.strftime("%Y-%m-%d %H:%M"))}</span></div>"""
    return _render(
        doc_type="exam_report",
        template=_template(db, "exam_report"),
        org_name=org_name,
        doc_title=DOC_TYPES["exam_report"],
        doc_no=f"BG{report.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 处方笺打印 ----------


@router.get("/prescriptions/{prescription_id}", response_class=HTMLResponse, response_model=str)
def print_prescription(
    prescription_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """处方笺打印版：患者信息、临床诊断、药品明细（用法用量）、审核状态与开方医师。"""
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    assert_patient_visible(db, user, rx.patient_id, resource="print:prescription")
    patient = db.get(Patient, rx.patient_id)
    org_name = _org_name(db, rx.org_id)
    items = (
        db.query(PrescriptionItem)
        .filter(PrescriptionItem.prescription_id == rx.id)
        .order_by(PrescriptionItem.id)
        .all()
    )
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">开方机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">开方时间</td><td>{_esc(rx.created_at.strftime("%Y-%m-%d %H:%M"))}</td></tr>'
        f'<tr><td class="k">临床诊断</td><td colspan="3">{_esc(rx.diagnosis_name) or "—"}</td></tr>'
    )
    rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(it.drug_name)}</td><td>{_esc(it.drug_code)}</td>"
        f"<td>{_esc(it.daily_dose)}</td><td>{_esc(it.days)}</td></tr>"
        for i, it in enumerate(items, start=1)
    ) or '<tr><td colspan="5">无药品明细</td></tr>'
    review = _esc(rx.review_comment) or "—"
    body = f"""
  <div class="section"><h3>R<sub>p</sub>（药品明细）</h3>
    <table class="items"><thead><tr><th>序号</th><th>药品名称</th><th>药品编码</th>
      <th>日剂量</th><th>用药天数</th></tr></thead><tbody>{rows}</tbody></table></div>
  <div class="section"><h3>审核意见（{_esc(RX_STATUS_NAMES.get(rx.status, rx.status))}）</h3>
    <div class="body">{review}</div></div>
  <div class="sign"><span>开方医师：{_esc(_user_name(db, rx.created_by)) or "—"}</span>
    <span>审核药师签名：____________</span><span>发药/核对：____________</span></div>"""
    return _render(
        doc_type="prescription",
        template=_template(db, "prescription"),
        org_name=org_name,
        doc_title=DOC_TYPES["prescription"],
        doc_no=f"CF{rx.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 检查/检验申请单打印 ----------


@router.get("/exam-requests/{request_id}", response_class=HTMLResponse, response_model=str)
def print_exam_request(
    request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """检查检验申请单打印版：患者信息、申请项目、临床资料、样本状态与申请医师。"""
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    assert_patient_visible(db, user, request.patient_id, resource="print:exam_request")
    patient = db.get(Patient, request.patient_id)
    org_name = _org_name(db, request.from_org_id)
    sample_names = {
        "": "—",
        "collected": "已采样",
        "in_transit": "转运中",
        "received": "中心已核收",
    }
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">申请机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">申请时间</td><td>{_esc(request.created_at.strftime("%Y-%m-%d %H:%M"))}</td></tr>'
        f'<tr><td class="k">检查类别</td><td>{_esc(CENTER_NAMES.get(request.center_type, request.center_type))}</td>'
        f'<td class="k">当前状态</td><td>{_esc(EXAM_STATUS_NAMES.get(request.status, request.status))}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>申请项目</h3>
    <table class="items"><thead><tr><th>项目编码</th><th>项目名称</th><th>样本状态</th></tr></thead>
    <tbody><tr><td>{_esc(request.item_code)}</td><td>{_esc(request.item_name)}</td>
      <td>{_esc(sample_names.get(request.sample_status, request.sample_status))}</td></tr></tbody></table></div>
  <div class="section"><h3>临床资料与检查目的</h3><div class="body">{_esc(request.clinical_info) or "—"}</div></div>
  <div class="sign"><span>申请医师：{_esc(_user_name(db, request.created_by)) or "—"}</span>
    <span>接收/执行签名：____________</span></div>"""
    return _render(
        doc_type="exam_request",
        template=_template(db, "exam_request"),
        org_name=org_name,
        doc_title=DOC_TYPES["exam_request"],
        doc_no=f"SQ{request.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 法定医学证明打印 ----------


@router.get("/certs/{cert_id}", response_class=HTMLResponse, response_model=str)
def print_cert(cert_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """出生/死亡医学证明与缺陷登记打印版：证明编号、当事人信息、事件日期与诊断说明。"""
    cert = db.get(MedicalCert, cert_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="证明不存在")
    if cert.patient_id:
        assert_patient_visible(db, user, cert.patient_id, resource="print:cert")
    else:
        # 出生/死亡证明可以不挂患者档案（如院外死亡登记）——退到签发机构可见性，
        # 不能因为"没有 patient_id"就变成谁都能打
        assert_org_visible(db, user, cert.org_id)
    org_name = _org_name(db, cert.org_id)
    patient = db.get(Patient, cert.patient_id) if cert.patient_id else None
    type_name = CERT_TYPE_NAMES.get(cert.cert_type, cert.cert_type)
    meta = (
        f'<tr><td class="k">姓名</td><td>{_esc(cert.name)}</td>'
        f'<td class="k">性别</td><td>{_esc(cert.gender)}</td></tr>'
        f'<tr><td class="k">事件日期</td><td>{_esc(cert.event_date)}</td>'
        f'<td class="k">证明类别</td><td>{_esc(type_name)}</td></tr>'
        f'<tr><td class="k">签发机构</td><td colspan="3">{_esc(org_name)}</td></tr>'
    ) + (_patient_rows(patient, user) if patient is not None else "")
    body = f"""
  <div class="section"><h3>诊断/说明</h3><div class="body">{_esc(cert.detail) or "—"}</div></div>
  <div class="sign"><span>签发人：{_esc(_user_name(db, cert.created_by)) or "—"}</span>
    <span>签发时间：{_esc(cert.created_at.strftime("%Y-%m-%d %H:%M"))}</span>
    <span>签发机构（章）：____________</span></div>"""
    return _render(
        doc_type="cert",
        template=_template(db, "cert"),
        org_name=org_name,
        doc_title=type_name,
        doc_no=_esc(cert.cert_no),
        meta_rows=meta,
        body_html=body,
    )


# ---------- 住院费用清单打印 ----------


def _get_admission(db: Session, admission_id: int) -> Admission:
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    return admission


@router.get("/inpatient-bills/{admission_id}", response_class=HTMLResponse, response_model=str)
def print_inpatient_bill(
    admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """住院费用清单打印版：按住院登记汇总费用明细（计费时价格快照口径）。"""
    admission = _get_admission(db, admission_id)
    assert_patient_visible(db, user, admission.patient_id, resource="print:inp_bill")
    patient = db.get(Patient, admission.patient_id)
    org_name = _org_name(db, admission.org_id)
    details = (
        db.query(BillDetail)
        .filter(BillDetail.admission_id == admission.id)
        .order_by(BillDetail.id)
        .all()
    )
    total = round(sum(d.amount for d in details), 2)
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">住院机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">入院时间</td><td>{_esc(admission.admitted_at.strftime("%Y-%m-%d %H:%M"))}</td></tr>'
        f'<tr><td class="k">入院诊断</td><td>{_esc(admission.diagnosis_name) or "—"}</td>'
        f'<td class="k">主管医师</td><td>{_esc(admission.doctor_name) or "—"}</td></tr>'
    )
    rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(d.item_code)}</td><td>{_esc(d.item_name)}</td>"
        f"<td>{d.unit_price:.2f}</td><td>{d.quantity}</td><td>{d.amount:.2f}</td>"
        f"<td>{_esc(d.created_at.strftime('%Y-%m-%d'))}</td></tr>"
        for i, d in enumerate(details, start=1)
    ) or '<tr><td colspan="7">本次住院暂无费用明细</td></tr>'
    body = f"""
  <div class="section"><h3>费用明细（共 {len(details)} 项，合计 {total:.2f} 元）</h3>
    <table class="items"><thead><tr><th>序号</th><th>项目编码</th><th>项目名称</th>
      <th>单价(元)</th><th>数量</th><th>金额(元)</th><th>计费日期</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
  <div class="sign"><span>费用合计：{total:.2f} 元</span><span>制单：____________</span></div>"""
    return _render(
        doc_type="inpatient_bill",
        template=_template(db, "inpatient_bill"),
        org_name=org_name,
        doc_title=DOC_TYPES["inpatient_bill"],
        doc_no=f"FY{admission.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 结算单打印 ----------


@router.get("/settlements/{settlement_id}", response_class=HTMLResponse, response_model=str)
def print_settlement(
    settlement_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """结算单打印版（住院/门诊同一版式）：总额、医保统筹支付与个人自付。"""
    settlement = db.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="结算单不存在")
    assert_patient_visible(db, user, settlement.patient_id, resource="print:settlement")
    patient = db.get(Patient, settlement.patient_id)
    org_name = _org_name(db, settlement.org_id)
    bill_type = BILL_TYPE_NAMES.get(settlement.bill_type, settlement.bill_type)
    detail_count = (
        db.query(BillDetail).filter(BillDetail.settlement_id == settlement.id).count()
    )
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">结算机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">结算类别</td><td>{_esc(bill_type)}结算</td></tr>'
        f'<tr><td class="k">结算时间</td><td>{_esc(settlement.created_at.strftime("%Y-%m-%d %H:%M"))}</td>'
        f'<td class="k">明细笔数</td><td>{detail_count}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>结算金额</h3>
    <table class="items"><thead><tr><th>费用总额(元)</th><th>医保统筹支付(元)</th>
      <th>个人自付(元)</th></tr></thead>
    <tbody><tr><td>{settlement.total_amount:.2f}</td><td>{settlement.insurance_pay:.2f}</td>
      <td>{settlement.self_pay:.2f}</td></tr></tbody></table></div>
  <div class="sign"><span>结算员：{_esc(_user_name(db, settlement.created_by)) or "—"}</span>
    <span>收讫（章）：____________</span></div>"""
    return _render(
        doc_type="settlement",
        template=_template(db, "settlement"),
        org_name=org_name,
        doc_title=f"{bill_type}{DOC_TYPES['settlement']}",
        doc_no=f"JS{settlement.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 病案首页打印 ----------


@router.get("/case-summaries/{admission_id}", response_class=HTMLResponse, response_model=str)
def print_case_summary(
    admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """病案首页打印版：现有最小数据集（出院诊断/手术/费用/转归/DRG）。"""
    admission = _get_admission(db, admission_id)
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission.id).first()
    if summary is None:
        raise HTTPException(status_code=404, detail="病案首页未填写")
    assert_patient_visible(db, user, admission.patient_id, resource="print:case_summary")
    patient = db.get(Patient, admission.patient_id)
    org_name = _org_name(db, admission.org_id)
    discharged = (
        admission.discharged_at.strftime("%Y-%m-%d %H:%M") if admission.discharged_at else "—"
    )
    drg = f"{summary.drg_code}（权重 {summary.drg_weight}）" if summary.drg_code else "未入组"
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">住院机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">主管医师</td><td>{_esc(admission.doctor_name) or "—"}</td></tr>'
        f'<tr><td class="k">入院时间</td><td>{_esc(admission.admitted_at.strftime("%Y-%m-%d %H:%M"))}</td>'
        f'<td class="k">出院时间</td><td>{_esc(discharged)}</td></tr>'
        f'<tr><td class="k">入院诊断</td><td>{_esc(admission.diagnosis_name) or "—"}</td>'
        f'<td class="k">DRG 分组</td><td>{_esc(drg)}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>出院诊断</h3><div class="body">{_esc(summary.discharge_diagnosis) or "—"}</div></div>
  <div class="section"><h3>手术及操作</h3><div class="body">{_esc(summary.operation) or "—"}</div></div>
  <div class="section"><h3>费用与转归</h3>
    <table class="items"><thead><tr><th>总费用(元)</th><th>其中药费(元)</th><th>转归</th></tr></thead>
    <tbody><tr><td>{summary.total_cost:.2f}</td><td>{summary.drug_cost:.2f}</td>
      <td>{_esc(summary.outcome)}</td></tr></tbody></table></div>
  <div class="section"><h3>备注</h3><div class="body">{_esc(summary.note) or "—"}</div></div>
  <div class="sign"><span>填写医师：{_esc(summary.created_by_name) or "—"}</span>
    <span>填写时间：{_esc(summary.created_at.strftime("%Y-%m-%d %H:%M"))}</span></div>"""
    return _render(
        doc_type="case_summary",
        template=_template(db, "case_summary"),
        org_name=org_name,
        doc_title=DOC_TYPES["case_summary"],
        doc_no=f"BA{admission.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 体检报告打印 ----------


@router.get("/checkups/{checkup_id}", response_class=HTMLResponse, response_model=str)
def print_checkup_report(
    checkup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """体检报告打印版：套餐、分项结果（异常标注）、汇总小结与总检结论。"""
    exam = db.get(PhysicalExam, checkup_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="体检记录不存在")
    assert_patient_visible(db, user, exam.patient_id, resource="print:checkup")
    patient = db.get(Patient, exam.patient_id)
    org_name = _org_name(db, exam.org_id)
    items = (
        db.query(CheckupItem)
        .filter(CheckupItem.checkup_id == exam.id)
        .order_by(CheckupItem.id)
        .all()
    )
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">体检机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">体检日期</td><td>{_esc(exam.exam_date) or "—"}</td></tr>'
        f'<tr><td class="k">体检套餐</td><td colspan="3">{_esc(exam.package_name)}</td></tr>'
    )
    abnormal_tag = '<span class="critical">异常 ↑</span>'
    rows = "".join(
        f"<tr><td>{i}</td><td>{_esc(it.item_name)}</td><td>{_esc(it.result_value)}</td>"
        f"<td>{_esc(it.unit) or '—'}</td><td>{_esc(it.ref_range) or '—'}</td>"
        f"<td>{abnormal_tag if it.abnormal else '正常'}</td></tr>"
        for i, it in enumerate(items, start=1)
    ) or '<tr><td colspan="6">无分项结果（存量记录仅有汇总小结）</td></tr>'
    review = (
        f'<div class="section"><h3>总检结论</h3><div class="body">{_esc(exam.final_conclusion)}</div></div>'
        f'<div class="sign"><span>总检医师：{_esc(exam.final_doctor)}</span></div>'
        if exam.final_conclusion
        else '<div class="section"><h3>总检结论</h3><div class="body">尚未总检</div></div>'
    )
    body = f"""
  <div class="section"><h3>分项结果</h3>
    <table class="items"><thead><tr><th>序号</th><th>项目</th><th>结果</th><th>单位</th>
      <th>参考范围</th><th>提示</th></tr></thead><tbody>{rows}</tbody></table></div>
  <div class="section"><h3>汇总小结</h3><div class="body">{_esc(exam.summary) or "—"}</div></div>
  <div class="section"><h3>异常项提示</h3><div class="body">{_esc(exam.abnormal_items) or "无"}</div></div>
  {review}"""
    return _render(
        doc_type="checkup_report",
        template=_template(db, "checkup_report"),
        org_name=org_name,
        doc_title=DOC_TYPES["checkup_report"],
        doc_no=f"TJ{exam.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 知情同意书打印 ----------


@router.get("/consents/{record_id}", response_class=HTMLResponse, response_model=str)
def print_consent(
    record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """知情同意书打印版：告知文本（按记录引用版本取 consent_texts）+ 同意要件。

    记录里的 text_version 找不到对应文本时（如线下纸质版本号），退到该场景
    当前 active 版并在文中标注版本号——举证以记录里的版本号为准。
    """
    record = db.get(ConsentRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="知情同意记录不存在")
    assert_patient_visible(db, user, record.patient_id, resource="print:consent")
    patient = db.get(Patient, record.patient_id)
    scene_name = CONSENT_SCENE_NAMES.get(record.scene, record.scene)
    text = (
        db.query(ConsentText)
        .filter(ConsentText.scene == record.scene, ConsentText.version == record.text_version)
        .first()
    )
    if text is None:
        text = (
            db.query(ConsentText)
            .filter(ConsentText.scene == record.scene, ConsentText.active.is_(True))
            .order_by(ConsentText.id.desc())
            .first()
        )
    text_html = (
        f'<div class="body">{_esc(text.content)}</div>'
        f"<p>（文本版本：{_esc(text.version)}；记录引用版本：{_esc(record.text_version) or '—'}）</p>"
        if text
        else f'<div class="body">该场景暂无告知文本存档（记录引用版本：{_esc(record.text_version) or "—"}）</div>'
    )
    guardian = (
        f'<tr><td class="k">监护人</td><td>{_esc(record.guardian_name)}</td>'
        f'<td class="k">与患者关系</td><td>{_esc(record.guardian_relation) or "—"}</td></tr>'
        if record.guardian_name
        else ""
    )
    revoked = (
        f'<p><span class="critical">该同意已于 {record.revoked_at.strftime("%Y-%m-%d %H:%M")} 撤回</span></p>'
        if record.revoked_at
        else ""
    )
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">同意场景</td><td>{_esc(scene_name)}</td>'
        f'<td class="k">采集方式</td><td>{_esc(CONSENT_METHOD_NAMES.get(record.method, record.method))}</td></tr>'
        f'<tr><td class="k">签署时间</td><td>{_esc(record.created_at.strftime("%Y-%m-%d %H:%M"))}</td>'
        f'<td class="k">佐证材料</td><td>{_esc(record.evidence) or "—"}</td></tr>'
        f"{guardian}"
    )
    body = f"""
  <div class="section"><h3>告知内容</h3>{text_html}</div>
  {revoked}
  <div class="sign"><span>签署人（患者/监护人）：____________</span>
    <span>经办人：{_esc(_user_name(db, record.operator_user_id)) or "—"}</span></div>"""
    return _render(
        doc_type="consent",
        template=_template(db, "consent"),
        org_name="",
        doc_title=f"{scene_name}知情同意书",
        doc_no=f"ZQ{record.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 疫苗接种证明打印 ----------


@router.get("/vaccinations/{record_id}", response_class=HTMLResponse, response_model=str)
def print_vaccine_cert(
    record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """疫苗接种证明打印版：按单条接种记录出证（剂次/批号/接种单位/接种者）。"""
    record = db.get(VaccinationRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="接种记录不存在")
    assert_patient_visible(db, user, record.patient_id, resource="print:vaccine_cert")
    patient = db.get(Patient, record.patient_id)
    org_name = _org_name(db, record.org_id)
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">接种单位</td><td>{_esc(org_name)}</td>'
        f'<td class="k">接种日期</td><td>{_esc(record.vaccinated_date) or "—"}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>接种信息</h3>
    <table class="items"><thead><tr><th>疫苗名称</th><th>疫苗编码</th><th>剂次</th>
      <th>批号</th><th>接种部位</th><th>接种者</th></tr></thead>
    <tbody><tr><td>{_esc(record.vaccine_name)}</td><td>{_esc(record.vaccine_code)}</td>
      <td>第 {record.dose_no} 剂</td><td>{_esc(record.batch_no) or "—"}</td>
      <td>{_esc(record.site) or "—"}</td><td>{_esc(record.vaccinator) or "—"}</td></tr></tbody>
    </table></div>
  <p>兹证明上述受种者已在本单位完成该剂次预防接种，特此证明。</p>
  <div class="sign"><span>接种单位（章）：____________</span>
    <span>登记时间：{_esc(record.created_at.strftime("%Y-%m-%d %H:%M"))}</span></div>"""
    return _render(
        doc_type="vaccine_cert",
        template=_template(db, "vaccine_cert"),
        org_name=org_name,
        doc_title=DOC_TYPES["vaccine_cert"],
        doc_no=f"YM{record.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 转诊单打印 ----------


@router.get("/referrals/{referral_id}", response_class=HTMLResponse, response_model=str)
def print_referral(
    referral_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """转诊单打印版：转出/转入机构、方向、事由与当前状态。"""
    referral = db.get(Referral, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    assert_patient_visible(db, user, referral.patient_id, resource="print:referral")
    patient = db.get(Patient, referral.patient_id)
    from_org = _org_name(db, referral.from_org_id)
    to_org = _org_name(db, referral.to_org_id)
    direction = REFERRAL_DIRECTION_NAMES.get(referral.direction, referral.direction)
    status = REFERRAL_STATUS_NAMES.get(referral.status, referral.status)
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">转出机构</td><td>{_esc(from_org)}</td>'
        f'<td class="k">转入机构</td><td>{_esc(to_org)}</td></tr>'
        f'<tr><td class="k">转诊方向</td><td>{_esc(direction)}</td>'
        f'<td class="k">当前状态</td><td>{_esc(status)}</td></tr>'
        f'<tr><td class="k">申请时间</td><td colspan="3">{_esc(referral.created_at.strftime("%Y-%m-%d %H:%M"))}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>转诊事由</h3><div class="body">{_esc(referral.reason) or "—"}</div></div>
  <div class="sign"><span>申请医师：{_esc(_user_name(db, referral.created_by)) or "—"}</span>
    <span>接诊签收：____________</span></div>"""
    return _render(
        doc_type="referral",
        template=_template(db, "referral"),
        org_name=from_org,
        doc_title=DOC_TYPES["referral"],
        doc_no=f"ZZ{referral.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 出院小结打印 ----------


@router.get("/discharge-summaries/{admission_id}", response_class=HTMLResponse, response_model=str)
def print_discharge_summary(
    admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """出院小结打印版：仅限已出院者（在院打印的"出院小结"没有出院时间，是伪造文书）。

    诊疗经过取出院病程记录（ProgressNote note_type="discharge"），未书写留"—"。
    """
    admission = _get_admission(db, admission_id)
    if admission.status != "discharged":
        raise HTTPException(status_code=409, detail="患者尚未出院，不可打印出院小结")
    assert_patient_visible(db, user, admission.patient_id, resource="print:discharge")
    patient = db.get(Patient, admission.patient_id)
    org_name = _org_name(db, admission.org_id)
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission.id).first()
    discharge_note = (
        db.query(ProgressNote)
        .filter(ProgressNote.admission_id == admission.id, ProgressNote.note_type == "discharge")
        .order_by(ProgressNote.id.desc())
        .first()
    )
    discharged = admission.discharged_at.strftime("%Y-%m-%d %H:%M") if admission.discharged_at else "—"
    days = (
        (admission.discharged_at - admission.admitted_at).days + 1
        if admission.discharged_at
        else "—"
    )
    meta = _patient_rows(patient, user) + (
        f'<tr><td class="k">住院机构</td><td>{_esc(org_name)}</td>'
        f'<td class="k">主管医师</td><td>{_esc(admission.doctor_name) or "—"}</td></tr>'
        f'<tr><td class="k">入院时间</td><td>{_esc(admission.admitted_at.strftime("%Y-%m-%d %H:%M"))}</td>'
        f'<td class="k">出院时间</td><td>{_esc(discharged)}</td></tr>'
        f'<tr><td class="k">住院天数</td><td>{_esc(days)} 天</td>'
        f'<td class="k">转归</td><td>{_esc(summary.outcome if summary else "") or "—"}</td></tr>'
    )
    body = f"""
  <div class="section"><h3>入院诊断</h3><div class="body">{_esc(admission.diagnosis_name) or "—"}</div></div>
  <div class="section"><h3>出院诊断</h3><div class="body">{_esc(summary.discharge_diagnosis if summary else "") or "—"}</div></div>
  <div class="section"><h3>手术及操作</h3><div class="body">{_esc(summary.operation if summary else "") or "—"}</div></div>
  <div class="section"><h3>诊疗经过（出院病程记录）</h3><div class="body">{_esc(discharge_note.content if discharge_note else "") or "—"}</div></div>
  <div class="sign"><span>主管医师：{_esc(admission.doctor_name) or "—"}</span>
    <span>打印核对：____________</span></div>"""
    return _render(
        doc_type="discharge_summary",
        template=_template(db, "discharge_summary"),
        org_name=org_name,
        doc_title=DOC_TYPES["discharge_summary"],
        doc_no=f"CY{admission.id:08d}",
        meta_rows=meta,
        body_html=body,
    )


# ---------- 打印模板维护（限管理员） ----------


class TemplateUpsert(BaseModel):
    doc_type: str = Field(pattern=_DOC_TYPE_PATTERN)
    header_org_name: str = ""
    footer_note: str = ""
    show_qr: bool = True


class TemplateOut(BaseModel):
    """模板行契约：字段与原手拼 dict 一一对应（未配置的类型 id 为 null）。"""

    id: int | None
    doc_type: str
    doc_type_name: str
    header_org_name: str
    footer_note: str
    show_qr: bool


def _template_out(t: PrintTemplate) -> dict:
    return {
        "id": t.id,
        "doc_type": t.doc_type,
        "doc_type_name": DOC_TYPES.get(t.doc_type, t.doc_type),
        "header_org_name": t.header_org_name,
        "footer_note": t.footer_note,
        "show_qr": t.show_qr,
    }


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)):
    """打印模板列表（含尚未配置的单据类型，便于前端一屏维护）。"""
    existing = {t.doc_type: _template_out(t) for t in db.query(PrintTemplate).all()}
    return [
        existing.get(
            doc_type,
            {
                "id": None,
                "doc_type": doc_type,
                "doc_type_name": name,
                "header_org_name": "",
                "footer_note": "",
                "show_qr": True,
            },
        )
        for doc_type, name in DOC_TYPES.items()
    ]


@router.put("/templates", response_model=TemplateOut, dependencies=[Depends(require_admin)])
def upsert_template(body: TemplateUpsert, db: Session = Depends(get_db)):
    """新增或更新打印模板（doc_type 唯一，幂等 upsert）。"""
    template, _ = upsert_unique(
        db,
        PrintTemplate,
        keys={"doc_type": body.doc_type},
        values={
            "header_org_name": body.header_org_name,
            "footer_note": body.footer_note,
            "show_qr": body.show_qr,
        },
    )
    return _template_out(template)
