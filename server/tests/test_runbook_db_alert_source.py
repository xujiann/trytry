"""运维手册让告警看运行环境概览的 `database.connected` 为假——这个值报不出假（P2-357）。

`GET /api/monitor/overview` 要登录（鉴权读用户表），还要读调度表，都在探活之前：库不通时整个请求先失败了，
`database.connected: false` 永远不会出现，按它配的告警永远不响。库连通以 `/api/health`（库不通 503、无需鉴权）为准。
"""
from pathlib import Path

RUNBOOK = (Path(__file__).resolve().parents[2] / "docs" / "运维手册.md").read_text(encoding="utf-8")


def _overview_row() -> str:
    return next(line for line in RUNBOOK.splitlines() if line.startswith("| 运行环境概览 |"))


def test_概览那一行不再拿database_connected当告警条件():
    row = _overview_row()
    assert "`database.connected` 为假、" not in row   # 修前的告警条件
    assert "/api/health" in row
