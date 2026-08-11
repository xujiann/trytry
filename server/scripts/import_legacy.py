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

用法：
    cd server
    python scripts/import_legacy.py <entity> <csv文件> [--dry-run]

    --dry-run  校验模式：完整执行解析/外键解析/幂等判定，报告 将导入/跳过/错误
               行数与错误明细，但**不落库**（事务回滚）。

退出码：0=全部行成功（含幂等跳过）；1=存在错误行；2=参数/文件错误。
数据库连接沿用 MEDPLAT_DATABASE_URL（与应用一致）。
"""
import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import ChronicPatient, Employee, Organization, Patient  # noqa: E402
from app.routers.patients import _generate_ehc_no  # noqa: E402

ORG_TYPES = {"lead_hospital", "township", "village", "public_health"}
ORG_LEVELS = {"city", "county", "township", "village"}
DISEASES = {"hypertension", "diabetes", "copd", "obesity", "hyperlipidemia"}


@dataclass
class ImportReport:
    entity: str
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (行号, 原因)

    def error(self, line_no: int, reason: str) -> None:
        self.errors.append((line_no, reason))

    def summary(self) -> str:
        mode = "【校验模式 dry-run，未落库】" if self.dry_run else "【已落库】"
        lines = [
            f"{mode} 实体={self.entity}",
            f"  将导入: {self.imported} 行" if self.dry_run else f"  已导入: {self.imported} 行",
            f"  幂等跳过(已存在): {self.skipped} 行",
            f"  错误: {len(self.errors)} 行",
        ]
        for line_no, reason in self.errors:
            lines.append(f"    - 第 {line_no} 行: {reason}")
        return "\n".join(lines)


def _require(row: dict, line_no: int, report: ImportReport, *cols: str) -> bool:
    missing = [c for c in cols if not (row.get(c) or "").strip()]
    if missing:
        report.error(line_no, f"缺少必填列: {', '.join(missing)}")
        return False
    return True


def _valid_date(value: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def import_organizations(db, rows, report: ImportReport) -> None:
    for line_no, row in rows:
        if not _require(row, line_no, report, "name", "org_type", "level"):
            continue
        name = row["name"].strip()
        org_type, level = row["org_type"].strip(), row["level"].strip()
        if org_type not in ORG_TYPES:
            report.error(line_no, f"org_type 非法: {org_type}（须为 {'/'.join(sorted(ORG_TYPES))}）")
            continue
        if level not in ORG_LEVELS:
            report.error(line_no, f"level 非法: {level}（须为 {'/'.join(sorted(ORG_LEVELS))}）")
            continue
        if db.query(Organization).filter(Organization.name == name).first():
            report.skipped += 1
            continue
        parent_id = None
        parent_name = (row.get("parent_name") or "").strip()
        if parent_name:
            parent = db.query(Organization).filter(Organization.name == parent_name).first()
            if parent is None:
                report.error(line_no, f"上级机构不存在: {parent_name}（请先导入上级机构行）")
                continue
            parent_id = parent.id
        db.add(
            Organization(
                name=name,
                org_type=org_type,
                level=level,
                parent_id=parent_id,
                address=(row.get("address") or "").strip(),
            )
        )
        db.flush()  # 同批后续行可解析到本行机构
        report.imported += 1


def import_patients(db, rows, report: ImportReport) -> None:
    seen_batch: set[str] = set()
    for line_no, row in rows:
        if not _require(row, line_no, report, "name", "id_card"):
            continue
        id_card = row["id_card"].strip()
        if len(id_card) not in (15, 18):
            report.error(line_no, f"身份证号长度非法: {id_card}（须 15 或 18 位）")
            continue
        birth_date = (row.get("birth_date") or "").strip()
        if birth_date and not _valid_date(birth_date):
            report.error(line_no, f"birth_date 格式非法: {birth_date}（须 YYYY-MM-DD）")
            continue
        if id_card in seen_batch:
            report.error(line_no, f"同批内身份证号重复: {id_card}")
            continue
        seen_batch.add(id_card)
        # EMPI 幂等：同身份证号视为同一人，不重复建档
        if db.query(Patient).filter(Patient.id_card == id_card).first():
            report.skipped += 1
            continue
        db.add(
            Patient(
                ehc_no=_generate_ehc_no(db),
                name=row["name"].strip(),
                id_card=id_card,
                gender=(row.get("gender") or "").strip() or "未知",
                birth_date=birth_date,
                phone=(row.get("phone") or "").strip(),
            )
        )
        db.flush()
        report.imported += 1


def import_chronic(db, rows, report: ImportReport) -> None:
    for line_no, row in rows:
        if not _require(row, line_no, report, "id_card", "disease", "managed_by_org"):
            continue
        disease = row["disease"].strip()
        if disease not in DISEASES:
            report.error(line_no, f"disease 非法: {disease}（须为 {'/'.join(sorted(DISEASES))}）")
            continue
        patient = db.query(Patient).filter(Patient.id_card == row["id_card"].strip()).first()
        if patient is None:
            report.error(line_no, f"患者不存在: {row['id_card'].strip()}（请先导入患者）")
            continue
        org_name = row["managed_by_org"].strip()
        org = db.query(Organization).filter(Organization.name == org_name).first()
        if org is None:
            report.error(line_no, f"管理机构不存在: {org_name}（请先导入机构）")
            continue
        level_raw = (row.get("level") or "").strip() or "1"
        if level_raw not in ("1", "2", "3"):
            report.error(line_no, f"level 非法: {level_raw}（须为 1/2/3）")
            continue
        next_due = (row.get("next_due") or "").strip()
        if next_due and not _valid_date(next_due):
            report.error(line_no, f"next_due 格式非法: {next_due}（须 YYYY-MM-DD）")
            continue
        existing = (
            db.query(ChronicPatient)
            .filter(ChronicPatient.patient_id == patient.id, ChronicPatient.disease == disease)
            .first()
        )
        if existing:
            report.skipped += 1
            continue
        db.add(
            ChronicPatient(
                patient_id=patient.id,
                disease=disease,
                level=int(level_raw),
                managed_by_org_id=org.id,
                next_due=next_due,
            )
        )
        db.flush()
        report.imported += 1


def import_employees(db, rows, report: ImportReport) -> None:
    for line_no, row in rows:
        if not _require(row, line_no, report, "org_name", "name"):
            continue
        org_name, name = row["org_name"].strip(), row["name"].strip()
        org = db.query(Organization).filter(Organization.name == org_name).first()
        if org is None:
            report.error(line_no, f"机构不存在: {org_name}（请先导入机构）")
            continue
        existing = (
            db.query(Employee).filter(Employee.org_id == org.id, Employee.name == name).first()
        )
        if existing:
            report.skipped += 1
            continue
        db.add(
            Employee(
                org_id=org.id,
                name=name,
                title=(row.get("title") or "").strip(),
                position=(row.get("position") or "").strip(),
            )
        )
        db.flush()
        report.imported += 1


IMPORTERS = {
    "organizations": import_organizations,
    "patients": import_patients,
    "chronic": import_chronic,
    "employees": import_employees,
}


def run_import(entity: str, csv_path: str | Path, dry_run: bool = False) -> ImportReport:
    """执行导入并返回报告。dry_run=True 时全程校验但事务回滚不落库。"""
    report = ImportReport(entity=entity, dry_run=dry_run)
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # 行号从 2 起（第 1 行为表头），便于对照原始文件定位错误
        rows = [(i, {k: (v or "") for k, v in row.items()}) for i, row in enumerate(reader, start=2)]

    Base.metadata.create_all(bind=engine)  # 空库直跑（生产环境应先 alembic upgrade head）
    db = SessionLocal()
    try:
        IMPORTERS[entity](db, rows, report)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="存量数据 CSV 批量导入（详见文件头注释）")
    parser.add_argument("entity", choices=sorted(IMPORTERS), help="导入实体")
    parser.add_argument("csv_file", help="CSV 文件路径（UTF-8，首行表头）")
    parser.add_argument("--dry-run", action="store_true", help="校验模式：报告结果但不落库")
    args = parser.parse_args()

    path = Path(args.csv_file)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    report = run_import(args.entity, path, dry_run=args.dry_run)
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
