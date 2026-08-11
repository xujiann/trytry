#!/usr/bin/env python3
"""标准字典导入工具：从 CSV 导入 ICD-10 全量诊断 / 药品目录等到统一编码字典。

复用 /api/dictionaries 的数据模型（CodeSystem/CodeEntry）直连数据库，
适合全量目录（如 ICD-10 三万余条）离线导入，绕开 HTTP 层开销。

字典类型：diagnosis(诊断 ICD-10) | drug(药品) | consumable(耗材) | charge(收费)
CSV 列（首行表头）：code,name

用法：
    cd server
    python scripts/import_dictionary.py diagnosis scripts/samples/icd10_diagnoses.csv
    python scripts/import_dictionary.py drug scripts/samples/drug_catalog.csv --dry-run

    --dry-run          校验模式：完整解析与幂等判定，报告统计但不落库（事务回滚）
    --progress-every N 每处理 N 行输出一次进度（默认 1000，0=关闭）

幂等：同一字典内已存在编码跳过（不覆盖名称）。
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
from app.models import CodeEntry, CodeSystem  # noqa: E402
from app.routers.dictionaries import SYSTEM_CODES  # noqa: E402


@dataclass
class ImportReport:
    system: str
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (行号, 原因)

    def error(self, line_no: int, reason: str) -> None:
        self.errors.append((line_no, reason))

    def summary(self) -> str:
        mode = "【校验模式 dry-run，未落库】" if self.dry_run else "【已落库】"
        lines = [
            f"{mode} 字典={self.system}({SYSTEM_CODES.get(self.system, self.system)})",
            f"  {'将导入' if self.dry_run else '已导入'}: {self.imported} 条",
            f"  幂等跳过(编码已存在): {self.skipped} 条",
            f"  错误: {len(self.errors)} 条",
        ]
        for line_no, reason in self.errors[:20]:
            lines.append(f"    第{line_no}行: {reason}")
        if len(self.errors) > 20:
            lines.append(f"    …另有 {len(self.errors) - 20} 行错误未列出")
        return "\n".join(lines)


def run_import(
    system_code: str,
    csv_path: Path | str,
    dry_run: bool = False,
    progress_every: int = 1000,
    out=print,
) -> ImportReport:
    """执行导入并返回统计报告；out 为进度输出回调（默认 print）。"""
    if system_code not in SYSTEM_CODES:
        raise ValueError(f"未知字典类型: {system_code}（可选：{'/'.join(SYSTEM_CODES)}）")
    csv_path = Path(csv_path)
    report = ImportReport(system=system_code, dry_run=dry_run)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
        if system is None:
            system = CodeSystem(code=system_code, name=SYSTEM_CODES[system_code])
            db.add(system)
            db.flush()
        existing = {
            code
            for (code,) in db.query(CodeEntry.code)
            .filter(CodeEntry.system_id == system.id)
            .all()
        }
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or not {"code", "name"} <= set(reader.fieldnames):
                raise ValueError("CSV 首行表头须包含列：code,name")
            processed = 0
            for line_no, row in enumerate(reader, start=2):
                processed += 1
                code = (row.get("code") or "").strip()
                name = (row.get("name") or "").strip()
                if not code:
                    report.error(line_no, "code 为空")
                elif not name:
                    report.error(line_no, f"name 为空（code={code}）")
                elif len(code) > 64:
                    report.error(line_no, f"code 超长（>64）：{code}")
                elif code in existing:
                    report.skipped += 1
                else:
                    db.add(CodeEntry(system_id=system.id, code=code, name=name[:256]))
                    existing.add(code)
                    report.imported += 1
                if progress_every and processed % progress_every == 0:
                    out(
                        f"  进度: 已处理 {processed} 行"
                        f"（导入 {report.imported} / 跳过 {report.skipped} / 错误 {len(report.errors)}）"
                    )
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="标准字典 CSV 导入（ICD-10 诊断/药品目录等）")
    parser.add_argument("system", choices=sorted(SYSTEM_CODES), help="字典类型")
    parser.add_argument("csv_file", help="CSV 文件路径（列：code,name）")
    parser.add_argument("--dry-run", action="store_true", help="校验模式，不落库")
    parser.add_argument(
        "--progress-every", type=int, default=1000, help="每 N 行输出进度（0=关闭）"
    )
    args = parser.parse_args()
    csv_path = Path(args.csv_file)
    if not csv_path.is_file():
        print(f"文件不存在: {csv_path}", file=sys.stderr)
        return 2
    try:
        report = run_import(
            args.system, csv_path, dry_run=args.dry_run, progress_every=args.progress_every
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
