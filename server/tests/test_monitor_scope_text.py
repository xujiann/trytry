"""运行监控页与运维手册把调用统计写死成「本实例进程内、重启即清零」：配了 Redis 之后是集群口径（P2-356）。

P1-24c 起配了 Redis 时计数取集群 hash（跨实例、跨重启，`counter_scope: "cluster"`），页面副标题却写死进程内口径，
同一页的面板标题（后端给的 `scope`）说的是集群——一页自相矛盾；运维手册也只写了进程内这一种。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MGMT = (ROOT / "server" / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "运维手册.md").read_text(encoding="utf-8")


def _render_monitor() -> str:
    start = MGMT.index("async function renderMonitor(")
    return MGMT[start:MGMT.index("\nasync function ", start + 1)]


def test_页面副标题的口径取自后端_不再写死进程内():
    body = _render_monitor()
    assert "进程重启即清零" not in body          # 修前写死在副标题里
    assert re.search(r"page-desc.*\$\{stats\.scope\}", body)


def test_运维手册写明两种口径():
    assert 'counter_scope: "cluster"' in RUNBOOK
    assert "未配置 Redis" in RUNBOOK and "跨实例、跨重启累计" in RUNBOOK
