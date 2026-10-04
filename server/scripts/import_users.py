#!/usr/bin/env python3
"""开办批量建号工具：从 CSV 批量创建平台用户（username 幂等）。

CSV 列（首行表头）：
    username    必填，3-64 位，登录名
    full_name   可空，姓名
    role        必填，内置角色（admin/director/doctor/pharmacist/public_health/
                operator）或 roles 表中已启用的自定义角色 key
    org_name    可空，所属机构名（按机构名解析外键；查无此机构报错行）
    password    可空，初始口令（≥8位且含字母数字）；留空/缺列则自动生成，
                生成的口令随报告输出（--credentials-csv 落盘），需线下发放并
                提示首登改密

用法：
    cd server
    python scripts/import_users.py scripts/samples/users.csv [--dry-run]
        [--credentials-csv 生成口令输出路径]

幂等：username 已存在跳过（不改角色不重置口令——开办工具只建号不改号）。
幂等只认**库里已有的**：同一个文件里 username 相同的后一行记错误行、点名与第几行相同，不计「幂等跳过」（P2-1271，
与存量导入 P2-732 同一口径）——两位同名「张伟」用同一个登录名，原先后一位没建号、口令清单里也没有他，回执却是
「幂等跳过 1、错误 0」、退出码 0。
退出码：0=全部行成功（含幂等跳过）；1=存在错误行；2=参数/文件错误。
数据库连接沿用 MEDPLAT_DATABASE_URL（与应用一致）。
"""
import argparse
import csv
import secrets
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, create_all_for_scripts  # noqa: E402
from app.deps import ROLE_NAMES  # noqa: E402  内置六角色（key→名称）
from app.models import Organization, Role, User  # noqa: E402
from app.security import hash_password, validate_password_strength  # noqa: E402
from import_legacy import _dup_in_batch  # noqa: E402  同批内重复与存量导入同一句（P2-1271）


@dataclass
class ImportReport:
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (行号, 原因)
    generated: list[tuple[str, str]] = field(default_factory=list)  # (username, 生成口令)

    def error(self, line_no: int, reason: str, row: dict | None = None) -> None:
        """`row` 与存量导入的报告同形，只为复用 `import_legacy._dup_in_batch`（P2-1271）；本工具不落 errors.csv，不用它。"""
        self.errors.append((line_no, reason))

    def summary(self) -> str:
        mode = "【校验模式 dry-run，未落库】" if self.dry_run else "【已落库】"
        lines = [
            f"{mode} 实体=users",
            f"  {'将建号' if self.dry_run else '已建号'}: {self.imported} 个",
            f"  幂等跳过(用户名已存在): {self.skipped} 个",
            f"  自动生成口令: {len(self.generated)} 个",
            f"  错误: {len(self.errors)} 行",
        ]
        for line_no, reason in self.errors[:50]:
            lines.append(f"    - 第 {line_no} 行: {reason}")
        return "\n".join(lines)


def _generate_password() -> str:
    """生成满足复杂度（≥8位、含字母与数字）的随机初始口令。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(12))
        if validate_password_strength(candidate) is None:
            return candidate


def run_import(
    csv_path: str | Path,
    dry_run: bool = False,
    out=print,
) -> ImportReport:
    """执行导入并返回报告。角色对内置六角色 + roles 表已启用自定义角色校验。"""
    report = ImportReport(dry_run=dry_run)
    create_all_for_scripts(dry_run)  # 只在开发环境、非 dry-run 时建表（P2-1089，ADR-0002）
    db = SessionLocal()
    try:
        # 集合预载：机构名→id、已存在用户名、合法角色（一次查询各一趟，行内零 SELECT）
        orgs = {name: oid for oid, name in db.query(Organization.id, Organization.name).all()}
        existing = {name for (name,) in db.query(User.username).all()}   # 只放库里原有的（P2-1271）
        seen_batch: dict[str, int] = {}   # 本文件里建了号的 username → 第几行
        valid_roles = set(ROLE_NAMES) | {
            key for (key,) in db.query(Role.key).filter(Role.active.is_(True)).all()
        }
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or not {"username", "role"} <= set(reader.fieldnames):
                raise ValueError("CSV 首行表头须包含列：username,role")
            for line_no, row in enumerate(reader, start=2):
                username = (row.get("username") or "").strip()
                role = (row.get("role") or "").strip()
                if not username or not role:
                    report.error(line_no, "缺少必填列: username/role")
                    continue
                if not (3 <= len(username) <= 64):
                    report.error(line_no, f"username 长度非法: {username}（须 3-64 位）")
                    continue
                if role not in valid_roles:
                    report.error(
                        line_no,
                        f"role 非法: {role}（须为 {'/'.join(sorted(ROLE_NAMES))} 或已启用自定义角色）",
                    )
                    continue
                org_id = None
                org_name = (row.get("org_name") or "").strip()
                if org_name:
                    org_id = orgs.get(org_name)
                    if org_id is None:
                        report.error(line_no, f"机构不存在: {org_name}（请先导入机构）")
                        continue
                # 同一文件里撞号的后一行记错误行、点名第几行（P2-1271）：原先本批刚建的号也塞进上面那个「已存在」集合，
                # 后一行被计成「幂等跳过」悄悄丢掉
                if _dup_in_batch(seen_batch, username, line_no, report, row, "用户名"):
                    continue
                if username in existing:
                    report.skipped += 1
                    continue
                password = (row.get("password") or "").strip()
                if password:
                    reason = validate_password_strength(password)
                    if reason:
                        report.error(line_no, f"password 不合规: {reason}")
                        continue
                else:
                    password = _generate_password()
                    report.generated.append((username, password))
                db.add(
                    User(
                        username=username,
                        password_hash=hash_password(password),
                        full_name=(row.get("full_name") or "").strip(),
                        role=role,
                        org_id=org_id,
                    )
                )
                seen_batch[username] = line_no
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
    parser = argparse.ArgumentParser(description="用户 CSV 批量建号（详见文件头注释）")
    parser.add_argument("csv_file", help="CSV 文件路径（列：username,full_name,role,org_name,password）")
    parser.add_argument("--dry-run", action="store_true", help="校验模式：报告结果但不落库")
    parser.add_argument(
        "--credentials-csv", default=None,
        help="自动生成口令的输出路径（默认 <输入>.credentials.csv；口令仅此一份，妥善发放）",
    )
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
    if report.generated and not args.dry_run:
        cred_path = Path(args.credentials_csv) if args.credentials_csv else path.with_suffix(
            path.suffix + ".credentials.csv"
        )
        with cred_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["username", "initial_password"])
            writer.writerows(report.generated)
        print(f"  自动生成口令已写入: {cred_path}（请线下发放并要求首登改密，发放后删除该文件）")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
