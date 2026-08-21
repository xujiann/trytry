#!/usr/bin/env python3
"""存量数据迁移工具：从 HIS/基卫等旧系统导出的 CSV 批量导入平台。

支持实体（CSV 列见 server/scripts/samples/ 样例）：
- organizations：机构（name 幂等）
    列：name, org_type(lead_hospital|township|village|public_health),
        level(city|county|township|village), parent_name(可空), address(可空)
- patients：患者（EMPI 按身份证号幂等，自动生成电子健康卡号）
    列：name, id_card, gender(可空), birth_date(可空 YYYY-MM-DD), phone(可空)
- chronic：慢病档案（患者身份证号 + 病种 幂等）
    列：id_card, disease(hypertension|diabetes|copd|obesity|hyperlipidemia),
        level(1-3 可空默认1), managed_by_org(机构名), next_due(可空 YYYY-MM-DD)
- employees：员工（机构名 + 姓名 幂等）
    列：org_name, name, title(可空), position(可空)
- encounters：就诊记录（患者+机构+就诊日期+诊断编码 幂等）
    列：id_card 或 ehc_no（二选一，患者外键解析）, org_name,
        visit_date(YYYY-MM-DD), encounter_type(可空 outpatient|inpatient 默认门诊),
        doctor_name(可空), diagnosis_code(可空), diagnosis_name(可空), summary(可空)
- prescriptions：处方+明细（每行一条明细，同一处方的明细行以 rx_no 相同且**相邻**
    表示；患者+机构+日期+药品编码集合 幂等——rx_no 仅用于分组，不落库）
    列：rx_no, id_card 或 ehc_no, org_name, rx_date(YYYY-MM-DD),
        diagnosis_name(可空), status(可空 approved|auto_passed 默认 approved),
        drug_code, drug_name, daily_dose(>0), days(可空默认1)
- settlements：结算单（患者+机构+类型+日期+总额 幂等；金额按 Money 口径校验：
    数值、非负、至多两位小数，且 总额 = 医保支付 + 自付）
    列：id_card 或 ehc_no, org_name, bill_type(outpatient|inpatient),
        settle_date(YYYY-MM-DD), total_amount, insurance_pay(可空默认0),
        self_pay(可空默认 总额-医保)
- admissions：住院登记（患者+机构+入院日期 幂等；病区/床位缺失时幂等自动建）
    列：id_card 或 ehc_no, org_name, ward_name, bed_no,
        admitted_at(YYYY-MM-DD), discharged_at(可空 YYYY-MM-DD，留空=在院),
        doctor_name(可空), diagnosis_name(可空)

用法：
    cd server
    python scripts/import_legacy.py <entity> <csv文件> [--dry-run]
        [--batch-size 1000] [--progress-every 1000] [--errors-csv 路径]
        [--operator admin]

    --dry-run        校验模式：完整执行解析/外键解析/幂等判定，报告 将导入/跳过/
                     错误 行数与错误明细，但**不落库**（事务回滚）。
    --batch-size     实导时每 N 个导入行提交一次事务（默认 1000），避免超大
                     文件单事务过长；dry-run 恒不提交。
    --progress-every 每处理 N 行输出一次进度（默认 1000，0=关闭）。
    --errors-csv     错误行明细输出路径（默认 <输入文件>.errors.csv，仅在
                     存在错误行时生成）；错误行跳过继续跑，不中断导入。
    --operator       写 created_by 的经办账号（prescriptions/settlements/
                     admissions 需要；默认 admin）。

性能：CSV 流式逐行读取（不整文件入内存）；外键与幂等键在导入前**一次查询
集合预载**（机构名/患者证件号→id、已存在业务键→集合），行内零 SELECT。

退出码：0=全部行成功（含幂等跳过）；1=存在错误行；2=参数/文件错误。
数据库连接沿用 MEDPLAT_DATABASE_URL（与应用一致）。
"""
import argparse
import csv
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Admission,
    Bed,
    ChronicPatient,
    Employee,
    Encounter,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    Settlement,
    User,
    Ward,
)

ORG_TYPES = {"lead_hospital", "township", "village", "public_health"}
ORG_LEVELS = {"city", "county", "township", "village"}
DISEASES = {"hypertension", "diabetes", "copd", "obesity", "hyperlipidemia"}
ENCOUNTER_TYPES = {"outpatient", "inpatient"}
RX_STATUSES = {"approved", "auto_passed"}
BILL_TYPES = {"outpatient", "inpatient"}


@dataclass
class ImportReport:
    entity: str
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (行号, 原因)
    # 错误行原始数据（供 errors.csv 落盘）：(行号, 原因, 原始行)
    error_rows: list[tuple[int, str, dict]] = field(default_factory=list)

    def error(self, line_no: int, reason: str, row: dict | None = None) -> None:
        self.errors.append((line_no, reason))
        self.error_rows.append((line_no, reason, row or {}))

    def summary(self) -> str:
        mode = "【校验模式 dry-run，未落库】" if self.dry_run else "【已落库】"
        lines = [
            f"{mode} 实体={self.entity}",
            f"  将导入: {self.imported} 行" if self.dry_run else f"  已导入: {self.imported} 行",
            f"  幂等跳过(已存在): {self.skipped} 行",
            f"  错误: {len(self.errors)} 行",
        ]
        for line_no, reason in self.errors[:50]:
            lines.append(f"    - 第 {line_no} 行: {reason}")
        if len(self.errors) > 50:
            lines.append(f"    …另有 {len(self.errors) - 50} 行错误未列出（见 errors.csv）")
        return "\n".join(lines)


class ImportContext:
    """批量提交与进度输出：importer 每导入一行调 checkpoint()，
    满 batch_size 提交一次（dry-run 恒不提交，最终回滚）。"""

    def __init__(self, db, report: ImportReport, *, dry_run: bool,
                 batch_size: int, progress_every: int, out, operator: str):
        self.db = db
        self.report = report
        self.dry_run = dry_run
        self.batch_size = max(1, batch_size)
        self.progress_every = progress_every
        self.out = out
        self.operator = operator
        self.processed = 0
        self._pending = 0

    def tick(self) -> None:
        self.processed += 1
        if self.progress_every and self.processed % self.progress_every == 0:
            r = self.report
            self.out(
                f"  进度: 已处理 {self.processed} 行"
                f"（导入 {r.imported} / 跳过 {r.skipped} / 错误 {len(r.errors)}）"
            )

    def checkpoint(self) -> None:
        self._pending += 1
        if not self.dry_run and self._pending >= self.batch_size:
            self.db.commit()
            self._pending = 0

    def operator_id(self) -> int:
        """created_by 经办账号解析（一次查询缓存）。"""
        if not hasattr(self, "_operator_id"):
            user = self.db.query(User).filter(User.username == self.operator).first()
            if user is None:
                raise ValueError(f"经办账号不存在: {self.operator}（--operator 指定，需已建号）")
            self._operator_id = user.id
        return self._operator_id


# ---------------------------------------------------------------- 通用工具


def _require(row: dict, line_no: int, report: ImportReport, *cols: str) -> bool:
    missing = [c for c in cols if not (row.get(c) or "").strip()]
    if missing:
        report.error(line_no, f"缺少必填列: {', '.join(missing)}", row)
        return False
    return True


def _valid_date(value: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _parse_money(raw: str) -> float | None:
    """Money 口径（Numeric(14,2)）：数值、非负、至多两位小数；非法返回 None。"""
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value < 0 or value != value.quantize(Decimal("0.01")):
        return None
    if value >= Decimal("1000000000000"):  # Numeric(14,2) 上限
        return None
    return float(value)


# —— 集合预载（一次查询建 dict/set，消除逐行 SELECT）——


def _orgs_by_name(db) -> dict[str, int]:
    return {name: oid for oid, name in db.query(Organization.id, Organization.name).all()}


def _patients_by_id_card(db) -> dict[str, int]:
    return {ic: pid for pid, ic in db.query(Patient.id, Patient.id_card).all()}


def _patients_by_ehc(db) -> dict[str, int]:
    return {ehc: pid for pid, ehc in db.query(Patient.id, Patient.ehc_no).all()}


def _resolve_patient(row: dict, line_no: int, report: ImportReport,
                     by_id_card: dict[str, int], by_ehc: dict[str, int]) -> int | None:
    """患者外键解析：身份证号优先，其次电子健康卡号。"""
    id_card = (row.get("id_card") or "").strip()
    ehc_no = (row.get("ehc_no") or "").strip()
    if id_card:
        pid = by_id_card.get(id_card)
        if pid is None:
            report.error(line_no, f"患者不存在: 身份证号 {id_card}（请先导入患者）", row)
        return pid
    if ehc_no:
        pid = by_ehc.get(ehc_no)
        if pid is None:
            report.error(line_no, f"患者不存在: 电子健康卡号 {ehc_no}（请先导入患者）", row)
        return pid
    report.error(line_no, "缺少患者标识列: id_card 或 ehc_no 至少填一列", row)
    return None


def _resolve_org(row: dict, line_no: int, report: ImportReport,
                 orgs: dict[str, int], col: str = "org_name") -> int | None:
    name = (row.get(col) or "").strip()
    org_id = orgs.get(name)
    if org_id is None:
        report.error(line_no, f"机构不存在: {name}（请先导入机构）", row)
    return org_id


def _date_key(value) -> str:
    """datetime/日期串 → YYYY-MM-DD 幂等键。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


# ---------------------------------------------------------------- 各实体导入器


def import_organizations(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    orgs = _orgs_by_name(db)
    for line_no, row in rows:
        if not _require(row, line_no, report, "name", "org_type", "level"):
            continue
        name = row["name"].strip()
        org_type, level = row["org_type"].strip(), row["level"].strip()
        if org_type not in ORG_TYPES:
            report.error(line_no, f"org_type 非法: {org_type}（须为 {'/'.join(sorted(ORG_TYPES))}）", row)
            continue
        if level not in ORG_LEVELS:
            report.error(line_no, f"level 非法: {level}（须为 {'/'.join(sorted(ORG_LEVELS))}）", row)
            continue
        if name in orgs:
            report.skipped += 1
            continue
        parent_id = None
        parent_name = (row.get("parent_name") or "").strip()
        if parent_name:
            parent_id = orgs.get(parent_name)
            if parent_id is None:
                report.error(line_no, f"上级机构不存在: {parent_name}（请先导入上级机构行）", row)
                continue
        org = Organization(
            name=name,
            org_type=org_type,
            level=level,
            parent_id=parent_id,
            address=(row.get("address") or "").strip(),
        )
        db.add(org)
        db.flush()  # 取 id，供同批后续行解析上级（flush 非查询，不破坏"行内零 SELECT"）
        orgs[name] = org.id
        report.imported += 1
        ctx.checkpoint()


def _generate_ehc_no(existing: set[str]) -> str:
    """电子健康卡号生成：对预载集合查重（替代逐行 SELECT），格式同平台建档。"""
    while True:
        candidate = "EHC" + secrets.token_hex(6).upper()
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def import_patients(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    existing_id_cards = set(_patients_by_id_card(db))
    existing_ehc = set(_patients_by_ehc(db))
    seen_batch: set[str] = set()
    for line_no, row in rows:
        if not _require(row, line_no, report, "name", "id_card"):
            continue
        id_card = row["id_card"].strip()
        if len(id_card) not in (15, 18):
            report.error(line_no, f"身份证号长度非法: {id_card}（须 15 或 18 位）", row)
            continue
        birth_date = (row.get("birth_date") or "").strip()
        if birth_date and not _valid_date(birth_date):
            report.error(line_no, f"birth_date 格式非法: {birth_date}（须 YYYY-MM-DD）", row)
            continue
        # 同批内重复单独报错（与库内已存在的"幂等跳过"语义区分，便于清洗源文件）
        if id_card in seen_batch:
            report.error(line_no, f"同批内身份证号重复: {id_card}", row)
            continue
        seen_batch.add(id_card)
        # EMPI 幂等：同身份证号视为同一人，不重复建档（集合预载，行内零 SELECT）
        if id_card in existing_id_cards:
            report.skipped += 1
            continue
        db.add(
            Patient(
                ehc_no=_generate_ehc_no(existing_ehc),
                name=row["name"].strip(),
                id_card=id_card,
                gender=(row.get("gender") or "").strip() or "未知",
                birth_date=birth_date,
                phone=(row.get("phone") or "").strip(),
            )
        )
        existing_id_cards.add(id_card)
        report.imported += 1
        ctx.checkpoint()


def import_chronic(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    patients = _patients_by_id_card(db)
    orgs = _orgs_by_name(db)
    existing = {
        (pid, disease)
        for pid, disease in db.query(ChronicPatient.patient_id, ChronicPatient.disease).all()
    }
    for line_no, row in rows:
        if not _require(row, line_no, report, "id_card", "disease", "managed_by_org"):
            continue
        disease = row["disease"].strip()
        if disease not in DISEASES:
            report.error(line_no, f"disease 非法: {disease}（须为 {'/'.join(sorted(DISEASES))}）", row)
            continue
        patient_id = patients.get(row["id_card"].strip())
        if patient_id is None:
            report.error(line_no, f"患者不存在: {row['id_card'].strip()}（请先导入患者）", row)
            continue
        org_id = orgs.get(row["managed_by_org"].strip())
        if org_id is None:
            report.error(line_no, f"管理机构不存在: {row['managed_by_org'].strip()}（请先导入机构）", row)
            continue
        level_raw = (row.get("level") or "").strip() or "1"
        if level_raw not in ("1", "2", "3"):
            report.error(line_no, f"level 非法: {level_raw}（须为 1/2/3）", row)
            continue
        next_due = (row.get("next_due") or "").strip()
        if next_due and not _valid_date(next_due):
            report.error(line_no, f"next_due 格式非法: {next_due}（须 YYYY-MM-DD）", row)
            continue
        if (patient_id, disease) in existing:
            report.skipped += 1
            continue
        db.add(
            ChronicPatient(
                patient_id=patient_id,
                disease=disease,
                level=int(level_raw),
                managed_by_org_id=org_id,
                next_due=next_due,
            )
        )
        existing.add((patient_id, disease))
        report.imported += 1
        ctx.checkpoint()


def import_employees(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    orgs = _orgs_by_name(db)
    existing = {(oid, name) for oid, name in db.query(Employee.org_id, Employee.name).all()}
    for line_no, row in rows:
        if not _require(row, line_no, report, "org_name", "name"):
            continue
        name = row["name"].strip()
        org_id = _resolve_org(row, line_no, report, orgs)
        if org_id is None:
            continue
        if (org_id, name) in existing:
            report.skipped += 1
            continue
        db.add(
            Employee(
                org_id=org_id,
                name=name,
                title=(row.get("title") or "").strip(),
                position=(row.get("position") or "").strip(),
            )
        )
        existing.add((org_id, name))
        report.imported += 1
        ctx.checkpoint()


def import_encounters(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    """就诊记录：created_at 取就诊日期；幂等键 患者+机构+就诊日期+诊断编码。"""
    by_id_card = _patients_by_id_card(db)
    by_ehc = _patients_by_ehc(db)
    orgs = _orgs_by_name(db)
    existing = {
        (pid, oid, _date_key(created), code)
        for pid, oid, created, code in db.query(
            Encounter.patient_id, Encounter.org_id, Encounter.created_at, Encounter.diagnosis_code
        ).all()
    }
    for line_no, row in rows:
        if not _require(row, line_no, report, "org_name", "visit_date"):
            continue
        visit_date = row["visit_date"].strip()
        if not _valid_date(visit_date):
            report.error(line_no, f"visit_date 格式非法: {visit_date}（须 YYYY-MM-DD）", row)
            continue
        encounter_type = (row.get("encounter_type") or "").strip() or "outpatient"
        if encounter_type not in ENCOUNTER_TYPES:
            report.error(
                line_no, f"encounter_type 非法: {encounter_type}（须为 outpatient/inpatient）", row
            )
            continue
        patient_id = _resolve_patient(row, line_no, report, by_id_card, by_ehc)
        if patient_id is None:
            continue
        org_id = _resolve_org(row, line_no, report, orgs)
        if org_id is None:
            continue
        diagnosis_code = (row.get("diagnosis_code") or "").strip()
        key = (patient_id, org_id, visit_date, diagnosis_code)
        if key in existing:
            report.skipped += 1
            continue
        db.add(
            Encounter(
                patient_id=patient_id,
                org_id=org_id,
                doctor_name=(row.get("doctor_name") or "").strip(),
                encounter_type=encounter_type,
                diagnosis_code=diagnosis_code,
                diagnosis_name=(row.get("diagnosis_name") or "").strip()[:256],
                summary=(row.get("summary") or "").strip()[:1024],
                created_at=datetime.fromisoformat(visit_date),
            )
        )
        existing.add(key)
        report.imported += 1
        ctx.checkpoint()


def import_prescriptions(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    """处方+明细：每行一条明细，rx_no 相同且相邻的行归入同一处方。

    幂等键 患者+机构+处方日期+药品编码集合（rx_no 不落库，仅分组用）。
    统计口径按**处方**计：imported/skipped 为处方数；组内任一行报错则整组不导
    （半张处方比没有更糟），错误按行记明细。
    """
    by_id_card = _patients_by_id_card(db)
    by_ehc = _patients_by_ehc(db)
    orgs = _orgs_by_name(db)
    operator_id = ctx.operator_id()
    # 预载既有处方业务键：处方头一次查询 + 明细一次查询，内存中拼键
    heads = {
        rx_id: (pid, oid, _date_key(created), diag)
        for rx_id, pid, oid, created, diag in db.query(
            Prescription.id, Prescription.patient_id, Prescription.org_id,
            Prescription.created_at, Prescription.diagnosis_name,
        ).all()
    }
    codes_by_rx: dict[int, set[str]] = {}
    for rx_id, drug_code in db.query(
        PrescriptionItem.prescription_id, PrescriptionItem.drug_code
    ).all():
        codes_by_rx.setdefault(rx_id, set()).add(drug_code)
    existing = {
        (pid, oid, day, frozenset(codes_by_rx.get(rx_id, set())))
        for rx_id, (pid, oid, day, _diag) in heads.items()
    }

    group: dict | None = None  # 当前处方组：header + items + tainted

    def flush_group() -> None:
        nonlocal group
        if group is None:
            return
        try:
            if group["tainted"]:
                return  # 组内有错误行，整组不导（错误已按行登记）
            key = (
                group["patient_id"], group["org_id"], group["rx_date"],
                frozenset(i["drug_code"] for i in group["items"]),
            )
            if key in existing:
                report.skipped += 1
                return
            rx = Prescription(
                patient_id=group["patient_id"],
                org_id=group["org_id"],
                diagnosis_name=group["diagnosis_name"],
                status=group["status"],
                created_by=operator_id,
                created_at=datetime.fromisoformat(group["rx_date"]),
            )
            db.add(rx)
            db.flush()  # 取处方 id 挂明细
            for item in group["items"]:
                db.add(PrescriptionItem(prescription_id=rx.id, **item))
            existing.add(key)
            report.imported += 1
            ctx.checkpoint()
        finally:
            group = None

    bad_rx_no: str | None = None  # 组首行即报错的 rx_no：其后续行不得另起残缺组
    for line_no, row in rows:
        rx_no = (row.get("rx_no") or "").strip()
        if group is not None and rx_no != group["rx_no"]:
            flush_group()
        if not _require(row, line_no, report, "rx_no", "org_name", "rx_date", "drug_code", "drug_name"):
            if group is not None and rx_no == group["rx_no"]:
                group["tainted"] = True
            elif rx_no:
                bad_rx_no = rx_no  # 组首行缺列：整组作废（半张处方比没有更糟）
            continue
        if group is None or rx_no != group.get("rx_no"):
            # 组首行：解析处方头
            rx_date = row["rx_date"].strip()
            status = (row.get("status") or "").strip() or "approved"
            patient_id = org_id = None
            tainted = False
            if not _valid_date(rx_date):
                report.error(line_no, f"rx_date 格式非法: {rx_date}（须 YYYY-MM-DD）", row)
                tainted = True
            elif status not in RX_STATUSES:
                report.error(line_no, f"status 非法: {status}（须为 {'/'.join(sorted(RX_STATUSES))}）", row)
                tainted = True
            else:
                patient_id = _resolve_patient(row, line_no, report, by_id_card, by_ehc)
                org_id = _resolve_org(row, line_no, report, orgs)
                tainted = patient_id is None or org_id is None
            group = {
                "rx_no": rx_no,
                "patient_id": patient_id,
                "org_id": org_id,
                "rx_date": rx_date,
                "status": status,
                "diagnosis_name": (row.get("diagnosis_name") or "").strip()[:256],
                "items": [],
                "tainted": tainted or rx_no == bad_rx_no,
            }
        # 明细行（组首行同时也是一条明细）
        daily_dose_raw = (row.get("daily_dose") or "").strip()
        days_raw = (row.get("days") or "").strip() or "1"
        try:
            daily_dose = float(daily_dose_raw)
        except ValueError:
            daily_dose = -1.0
        if daily_dose <= 0:
            report.error(line_no, f"daily_dose 非法: {daily_dose_raw}（须为正数）", row)
            group["tainted"] = True
            continue
        if not days_raw.isdigit() or int(days_raw) < 1:
            report.error(line_no, f"days 非法: {days_raw}（须为正整数）", row)
            group["tainted"] = True
            continue
        group["items"].append(
            {
                "drug_code": row["drug_code"].strip(),
                "drug_name": row["drug_name"].strip()[:128],
                "daily_dose": daily_dose,
                "days": int(days_raw),
            }
        )
    flush_group()


def import_settlements(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    """结算单：金额按 Money 口径校验；幂等键 患者+机构+类型+结算日期+总额。"""
    by_id_card = _patients_by_id_card(db)
    by_ehc = _patients_by_ehc(db)
    orgs = _orgs_by_name(db)
    operator_id = ctx.operator_id()
    existing = {
        (pid, oid, bill_type, _date_key(created), round(total or 0, 2))
        for pid, oid, bill_type, created, total in db.query(
            Settlement.patient_id, Settlement.org_id, Settlement.bill_type,
            Settlement.created_at, Settlement.total_amount,
        ).all()
    }
    for line_no, row in rows:
        if not _require(row, line_no, report, "org_name", "bill_type", "settle_date", "total_amount"):
            continue
        bill_type = row["bill_type"].strip()
        if bill_type not in BILL_TYPES:
            report.error(line_no, f"bill_type 非法: {bill_type}（须为 outpatient/inpatient）", row)
            continue
        settle_date = row["settle_date"].strip()
        if not _valid_date(settle_date):
            report.error(line_no, f"settle_date 格式非法: {settle_date}（须 YYYY-MM-DD）", row)
            continue
        total = _parse_money(row["total_amount"].strip())
        if total is None:
            report.error(
                line_no,
                f"total_amount 非法: {row['total_amount'].strip()}（须为非负、至多两位小数的金额）",
                row,
            )
            continue
        insurance_raw = (row.get("insurance_pay") or "").strip() or "0"
        insurance = _parse_money(insurance_raw)
        if insurance is None or insurance > total:
            report.error(line_no, f"insurance_pay 非法: {insurance_raw}（须为不超过总额的金额）", row)
            continue
        self_raw = (row.get("self_pay") or "").strip()
        self_pay = round(total - insurance, 2) if not self_raw else _parse_money(self_raw)
        if self_pay is None:
            report.error(line_no, f"self_pay 非法: {self_raw}（须为非负、至多两位小数的金额）", row)
            continue
        if round(insurance + self_pay, 2) != round(total, 2):
            report.error(
                line_no,
                f"金额不平: 总额 {total} ≠ 医保 {insurance} + 自付 {self_pay}",
                row,
            )
            continue
        patient_id = _resolve_patient(row, line_no, report, by_id_card, by_ehc)
        if patient_id is None:
            continue
        org_id = _resolve_org(row, line_no, report, orgs)
        if org_id is None:
            continue
        key = (patient_id, org_id, bill_type, settle_date, round(total, 2))
        if key in existing:
            report.skipped += 1
            continue
        db.add(
            Settlement(
                patient_id=patient_id,
                org_id=org_id,
                bill_type=bill_type,
                total_amount=total,
                insurance_pay=insurance,
                self_pay=self_pay,
                created_by=operator_id,
                created_at=datetime.fromisoformat(settle_date),
            )
        )
        existing.add(key)
        report.imported += 1
        ctx.checkpoint()


def import_admissions(db, rows, report: ImportReport, ctx: ImportContext) -> None:
    """住院登记：幂等键 患者+机构+入院日期；病区/床位缺失时幂等自动建。

    留空 discharged_at 的行按"在院"导入并占用床位（同床两条在院记录报错）；
    历史已出院记录不占床。
    """
    by_id_card = _patients_by_id_card(db)
    by_ehc = _patients_by_ehc(db)
    orgs = _orgs_by_name(db)
    operator_id = ctx.operator_id()
    wards = {(oid, name): wid for wid, oid, name in db.query(Ward.id, Ward.org_id, Ward.name).all()}
    beds = {
        (wid, no): (bid, status)
        for bid, wid, no, status in db.query(Bed.id, Bed.ward_id, Bed.bed_no, Bed.status).all()
    }
    existing = {
        (pid, oid, _date_key(admitted))
        for pid, oid, admitted in db.query(
            Admission.patient_id, Admission.org_id, Admission.admitted_at
        ).all()
    }
    for line_no, row in rows:
        if not _require(row, line_no, report, "org_name", "ward_name", "bed_no", "admitted_at"):
            continue
        admitted_at = row["admitted_at"].strip()
        if not _valid_date(admitted_at):
            report.error(line_no, f"admitted_at 格式非法: {admitted_at}（须 YYYY-MM-DD）", row)
            continue
        discharged_at = (row.get("discharged_at") or "").strip()
        if discharged_at and (not _valid_date(discharged_at) or discharged_at < admitted_at):
            report.error(
                line_no, f"discharged_at 非法: {discharged_at}（须 YYYY-MM-DD 且不早于入院日）", row
            )
            continue
        patient_id = _resolve_patient(row, line_no, report, by_id_card, by_ehc)
        if patient_id is None:
            continue
        org_id = _resolve_org(row, line_no, report, orgs)
        if org_id is None:
            continue
        key = (patient_id, org_id, admitted_at)
        if key in existing:
            report.skipped += 1
            continue
        ward_name, bed_no = row["ward_name"].strip(), row["bed_no"].strip()
        ward_id = wards.get((org_id, ward_name))
        if ward_id is None:
            ward = Ward(org_id=org_id, name=ward_name)
            db.add(ward)
            db.flush()
            ward_id = wards[(org_id, ward_name)] = ward.id
        bed_entry = beds.get((ward_id, bed_no))
        if bed_entry is None:
            bed = Bed(ward_id=ward_id, bed_no=bed_no)
            db.add(bed)
            db.flush()
            bed_entry = beds[(ward_id, bed_no)] = (bed.id, "free")
        bed_id, bed_status = bed_entry
        in_hospital = not discharged_at
        if in_hospital:
            if bed_status == "occupied":
                report.error(line_no, f"床位已被占用: {ward_name}/{bed_no}（在院记录不可同床）", row)
                continue
            # 在院记录占床：条件 UPDATE 走内存态维护，批末统一提交
            db.query(Bed).filter(Bed.id == bed_id).update(
                {Bed.status: "occupied"}, synchronize_session=False
            )
            beds[(ward_id, bed_no)] = (bed_id, "occupied")
        db.add(
            Admission(
                patient_id=patient_id,
                org_id=org_id,
                ward_id=ward_id,
                bed_id=bed_id,
                doctor_name=(row.get("doctor_name") or "").strip(),
                diagnosis_name=(row.get("diagnosis_name") or "").strip()[:256],
                status="admitted" if in_hospital else "discharged",
                admitted_at=datetime.fromisoformat(admitted_at),
                discharged_at=datetime.fromisoformat(discharged_at) if discharged_at else None,
                created_by=operator_id,
            )
        )
        existing.add(key)
        report.imported += 1
        ctx.checkpoint()


IMPORTERS = {
    "organizations": import_organizations,
    "patients": import_patients,
    "chronic": import_chronic,
    "employees": import_employees,
    "encounters": import_encounters,
    "prescriptions": import_prescriptions,
    "settlements": import_settlements,
    "admissions": import_admissions,
}


def _write_errors_csv(path: Path, fieldnames: list[str], report: ImportReport) -> None:
    """错误行落盘：原始列 + line_no + error，便于修正后重新导入该文件。"""
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*fieldnames, "line_no", "error"], extrasaction="ignore")
        writer.writeheader()
        for line_no, reason, row in report.error_rows:
            writer.writerow({**row, "line_no": line_no, "error": reason})


def run_import(
    entity: str,
    csv_path: str | Path,
    dry_run: bool = False,
    *,
    batch_size: int = 1000,
    progress_every: int = 1000,
    errors_csv: str | Path | None = None,
    operator: str = "admin",
    out=print,
) -> ImportReport:
    """执行导入并返回报告。dry_run=True 时全程校验但事务回滚不落库。

    CSV 流式逐行处理；实导每 batch_size 个导入行提交一次；错误行不中断，
    结束后（有错且指定 errors_csv 时）落盘错误明细。
    """
    report = ImportReport(entity=entity, dry_run=dry_run)
    Base.metadata.create_all(bind=engine)  # 空库直跑（生产环境应先 alembic upgrade heads）
    db = SessionLocal()
    fieldnames: list[str] = []
    try:
        ctx = ImportContext(
            db, report, dry_run=dry_run, batch_size=batch_size,
            progress_every=progress_every, out=out, operator=operator,
        )
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])

            def stream():
                # 行号从 2 起（第 1 行为表头），便于对照原始文件定位错误
                for i, row in enumerate(reader, start=2):
                    yield i, {k: (v or "") for k, v in row.items() if k is not None}
                    ctx.tick()

            IMPORTERS[entity](db, stream(), report, ctx)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if errors_csv and report.error_rows:
        _write_errors_csv(Path(errors_csv), fieldnames, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="存量数据 CSV 批量导入（详见文件头注释）")
    parser.add_argument("entity", choices=sorted(IMPORTERS), help="导入实体")
    parser.add_argument("csv_file", help="CSV 文件路径（UTF-8，首行表头）")
    parser.add_argument("--dry-run", action="store_true", help="校验模式：报告结果但不落库")
    parser.add_argument("--batch-size", type=int, default=1000, help="每 N 个导入行提交一次（默认 1000）")
    parser.add_argument("--progress-every", type=int, default=1000, help="每 N 行输出进度（0=关闭）")
    parser.add_argument("--errors-csv", default=None, help="错误行输出路径（默认 <输入>.errors.csv）")
    parser.add_argument("--operator", default="admin", help="created_by 经办账号（默认 admin）")
    args = parser.parse_args()

    path = Path(args.csv_file)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    errors_csv = Path(args.errors_csv) if args.errors_csv else path.with_suffix(path.suffix + ".errors.csv")
    try:
        report = run_import(
            args.entity, path, dry_run=args.dry_run, batch_size=args.batch_size,
            progress_every=args.progress_every, errors_csv=errors_csv, operator=args.operator,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.summary())
    if report.errors:
        print(f"  错误行明细已写入: {errors_csv}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
