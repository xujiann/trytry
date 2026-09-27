"""运行监控页与运维手册把调用统计写死成「本实例进程内、重启即清零」：配了 Redis 之后是集群口径（P2-356）。

P1-24c 起配了 Redis 时计数取集群 hash（跨实例、跨重启，`counter_scope: "cluster"`），页面副标题却写死进程内口径，
同一页的面板标题（后端给的 `scope`）说的是集群——一页自相矛盾；运维手册也只写了进程内这一种。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MGMT = (ROOT / "server" / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "运维手册.md").read_text(encoding="utf-8")
MANUAL = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")


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


def test_用户手册也写明两种口径():
    """P2-469（P2-356 余项）：用户手册的运行监控一行原先仍写死「调用统计是本实例进程内数据，重启即清零」。"""
    line = next(row for row in MANUAL.splitlines() if row.startswith("| 运行监控 |"))
    assert "调用统计是本实例进程内数据，重启即清零" not in line
    assert "未配置 Redis" in line and "集群口径" in line and "跨实例、跨重启累计" in line


def test_概览的口径不提它不带的调用统计(client, admin):
    """`/api/monitor/overview` 不带调用统计（那在 api-stats），原先的口径文案却写「调用统计……为进程内数据」。"""
    scope = client.get("/api/monitor/overview", headers=admin).json()["scope"]
    assert "调用统计" not in scope and "调度器状态取自数据库" in scope
