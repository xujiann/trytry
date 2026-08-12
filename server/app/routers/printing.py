"""报告打印（指引①②③④"报告打印"）：服务端渲染 A4 版式可打印 HTML。

- 覆盖单据：检查报告、处方笺、检查/检验申请单、法定医学证明
- 患者敏感字段脱敏规则与业务接口完全一致（复用 privacy 模块，非 admin 掩码）
- 版式：@page A4 + @media print，打印时隐藏操作按钮，页脚带打印时间
- 模板可配：PrintTemplate（doc_type 唯一）配置抬头机构名、页脚说明与二维码开关
"""
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_local
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import (
    ExamReport,
    ExamRequest,
    MedicalCert,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    PrintTemplate,
    User,
)
from ..privacy import mask_id_card, mask_phone

router = APIRouter(prefix="/api/print", tags=["报告打印"], dependencies=[Depends(get_current_user)])

DOC_TYPES = {
    "exam_report": "检查检验报告单",
    "prescription": "处方笺",
    "exam_request": "检查检验申请单",
    "cert": "医学证明",
}

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


@router.get("/exam-reports/{report_id}", response_class=HTMLResponse)
def print_exam_report(
    report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """检查检验报告单打印版：机构抬头、患者信息、项目、所见、结论、危急值标记与报告医师。"""
    report = db.get(ExamReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    request = db.get(ExamRequest, report.request_id)
    patient = db.get(Patient, request.patient_id) if request else None
    org_name = _org_name(db, request.from_org_id) if request else ""
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


@router.get("/prescriptions/{prescription_id}", response_class=HTMLResponse)
def print_prescription(
    prescription_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """处方笺打印版：患者信息、临床诊断、药品明细（用法用量）、审核状态与开方医师。"""
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
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


@router.get("/exam-requests/{request_id}", response_class=HTMLResponse)
def print_exam_request(
    request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """检查检验申请单打印版：患者信息、申请项目、临床资料、样本状态与申请医师。"""
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
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


@router.get("/certs/{cert_id}", response_class=HTMLResponse)
def print_cert(cert_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """出生/死亡医学证明与缺陷登记打印版：证明编号、当事人信息、事件日期与诊断说明。"""
    cert = db.get(MedicalCert, cert_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="证明不存在")
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


# ---------- 打印模板维护（限管理员） ----------


class TemplateUpsert(BaseModel):
    doc_type: str = Field(pattern="^(exam_report|prescription|exam_request|cert)$")
    header_org_name: str = ""
    footer_note: str = ""
    show_qr: bool = True


def _template_out(t: PrintTemplate) -> dict:
    return {
        "id": t.id,
        "doc_type": t.doc_type,
        "doc_type_name": DOC_TYPES.get(t.doc_type, t.doc_type),
        "header_org_name": t.header_org_name,
        "footer_note": t.footer_note,
        "show_qr": t.show_qr,
    }


@router.get("/templates")
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


@router.put("/templates", dependencies=[Depends(require_admin)])
def upsert_template(body: TemplateUpsert, db: Session = Depends(get_db)):
    """新增或更新打印模板（doc_type 唯一，幂等 upsert）。"""
    template = _template(db, body.doc_type)
    if template is None:
        template = PrintTemplate(doc_type=body.doc_type)
        db.add(template)
    template.header_org_name = body.header_org_name
    template.footer_note = body.footer_note
    template.show_qr = body.show_qr
    db.commit()
    db.refresh(template)
    return _template_out(template)
