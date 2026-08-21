"""WAL 归档与 PG 恢复演练脚本（工程包 P2）：可执行物的最低保障。

无真 PG 的 CI 里能钉住的是：脚本语法合法（sh -n）、dry-run 可跑通且打印全部
演练步骤、缺 pgbackrest 时给安装指引并以非零码退出、配置样例关键项齐全。
在真实 PG 上跑通前，RPO≤15min **不算达标**（见运维手册"WAL 归档与 PITR"节）。
"""
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_scripts_pass_sh_syntax_check():
    for name in ("backup_pg_pitr.sh", "restore_drill_pg.sh"):
        proc = subprocess.run(
            ["sh", "-n", str(SCRIPTS / name)], capture_output=True, text=True
        )
        assert proc.returncode == 0, f"{name} 语法错误：{proc.stderr}"


def test_backup_script_requires_pgbackrest_with_guidance():
    """无 pgbackrest 的环境：给安装指引，退出码非 0（备份没跑就得让 cron 报错）。"""
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "backup_pg_pitr.sh"), "full"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    if proc.returncode == 0:  # 环境里真装了 pgbackrest（如运维机），此项不适用
        return
    assert proc.returncode == 3
    assert "pgbackrest" in proc.stderr
    assert "archive-push" in proc.stderr  # 指引必须覆盖 WAL 归档配置


def test_restore_drill_dry_run_prints_all_steps():
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "restore_drill_pg.sh"), "--dry-run"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for expected in (
        "restore",            # 恢复
        "alembic",            # 迁移版本校验
        "count(*)",           # 健康查询
        "stop",               # 停临时实例
        "dry-run 结束",
    ):
        assert expected in proc.stdout, f"dry-run 步骤缺失：{expected}\n{proc.stdout}"


def test_pgbackrest_conf_example_covers_essentials():
    conf = (SCRIPTS / "pgbackrest.conf.example").read_text(encoding="utf-8")
    assert "[medplat]" in conf              # stanza 名与脚本默认一致
    assert "repo1-retention-full" in conf   # 保留策略
    assert "repo1-cipher-type" in conf      # 备份含居民敏感信息，必须加密
    assert "archive_command" in conf        # postgresql.conf 接线说明
    assert "archive_timeout" in conf        # RPO 兜底
