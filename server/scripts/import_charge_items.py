#!/usr/bin/env python3
"""开办批量工具：收费项目目录 CSV 批量导入（code 幂等）。

CSV 列（首行表头）：
    code      必填，≤64 位，收费项目编码（关联四统一 charge 字典）
    name      必填，≤128 位，项目名称
    category  可空，drug|exam|treatment|bed|other（默认 other）
    price     必填，单价（Money 口径：正数、至多两位小数）
    active    可空，true|false（默认 true）

与 /api/billing/charge-items 建目录接口同一管控口径：charge 字典已配置条目时，
仅字典内编码可入目录（编码不在字典 → 错误行）。

用法：
    cd server
    python scripts/import_charge_items.py scripts/samples/charge_items.csv [--dry-run]

幂等：code 已存在跳过（不覆盖名称与价格——调价走调价接口留痕，不走导入）。
退出码：0=全部行成功（含幂等跳过）；1=存在错误行；2=参数/文件错误。
数据库连接沿用 MEDPLAT_DATABASE_URL（与应用一致）。
"""
import argparse
import csv
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import ChargeItem, CodeEntry, CodeSystem  # noqa: E402

CATEGORIES = {"drug", "exam", "treatment", "bed", "other"}


@dataclass
class ImportReport:
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (行号, 原因)

    def error(self, line_no: int, reason: str) -> None:
        self.errors.append((line_no, reason))

    def summary(self) -> str:
        mode = "【校验模式 dry-run，未落库】" if self.dry_run else "【已落库】"
        lines = [
            f"{mode} 实体=charge_items",
            f"  {'将导入' if self.dry_run else '已导入'}: {self.imported} 条",
            f"  幂等跳过(编码已存在): {self.skipped} 条",
            f"  错误: {len(self.errors)} 条",
        ]
        for line_no, reason in self.errors[:50]:
            lines.append(f"    - 第 {line_no} 行: {reason}")
        return "\n".join(lines)


def _parse_price(raw: str) -> float | None:
    """Money 口径（Numeric(14,2)）：正数、至多两位小数；非法返回 None。"""
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        return None
    if value >= Decimal("1000000000000"):
        return None
    return float(value)


def run_import(csv_path: str | Path, dry_run: bool = False) -> ImportReport:
    """执行导入并返回报告。dry_run=True 时全程校验但事务回滚不落库。"""
    report = ImportReport(dry_run=dry_run)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # 集合预载：已有目录编码 + charge 字典编码（行内零 SELECT）
        existing = {code for (code,) in db.query(ChargeItem.code).all()}
        charge_system = db.query(CodeSystem).filter(CodeSystem.code == "charge").first()
        dict_codes: set[str] | None = None  # None=字典未配置，不管控
        if charge_system is not None:
            codes = {
                code
                for (code,) in db.query(CodeEntry.code).filter(
                    CodeEntry.system_id == charge_system.id
                )
            }
            dict_codes = codes or None
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or not {"code", "name", "price"} <= set(reader.fieldnames):
                raise ValueError("CSV 首行表头须包含列：code,name,price")
            for line_no, row in enumerate(reader, start=2):
                code = (row.get("code") or "").strip()
                name = (row.get("name") or "").strip()
                if not code or not name:
                    report.error(line_no, "缺少必填列: code/name")
                    continue
                if len(code) > 64:
                    report.error(line_no, f"code 超长（>64）：{code}")
                    continue
                category = (row.get("category") or "").strip() or "other"
                if category not in CATEGORIES:
                    report.error(
                        line_no, f"category 非法: {category}（须为 {'/'.join(sorted(CATEGORIES))}）"
                    )
                    continue
                price = _parse_price((row.get("price") or "").strip())
                if price is None:
                    report.error(
                        line_no,
                        f"price 非法: {(row.get('price') or '').strip()}（须为正数、至多两位小数）",
                    )
                    continue
                active_raw = (row.get("active") or "").strip().lower() or "true"
                if active_raw not in ("true", "false"):
                    report.error(line_no, f"active 非法: {active_raw}（须为 true/false）")
                    continue
                if dict_codes is not None and code not in dict_codes:
                    report.error(line_no, f"编码不在收费字典中: {code}（先维护 charge 字典）")
                    continue
                if code in existing:
                    report.skipped += 1
                    continue
                db.add(
                    ChargeItem(
                        code=code,
                        name=name[:128],
                        category=category,
                        price=price,
                        active=(active_raw == "true"),
                    )
                )
                existing.add(code)
                report.imported += 1
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
    parser = argparse.ArgumentParser(description="收费项目目录 CSV 批量导入（详见文件头注释）")
    parser.add_argument("csv_file", help="CSV 文件路径（列：code,name,category,price,active）")
    parser.add_argument("--dry-run", action="store_true", help="校验模式：报告结果但不落库")
    args = parser.parse_args()
    path = Path(args.csv_file)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    try:
        report = run_import(path, dry_run=args.dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
