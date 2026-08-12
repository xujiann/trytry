"""块3：压测脚本冒烟测试——用 ASGITransport 打进程内应用，验证脚本可运行且产出指标。"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from loadtest import format_results, run_benchmark  # noqa: E402


@pytest.fixture(scope="module")
def client():
    reset_database()
    # TestClient 上下文触发 lifespan（种子化 admin 账号）
    with TestClient(app) as c:
        yield c


def test_benchmark_smoke(client):
    transport = httpx.ASGITransport(app=app)
    results = asyncio.run(
        run_benchmark(
            "http://testserver",
            concurrency=2,
            requests_per_scenario=4,
            transport=transport,
        )
    )
    assert [r["scenario"] for r in results] == [
        "login", "patients", "exam_order",
        # 阶段一~五新增的重接口：查房高频写 + 三个聚合/报表
        "progress_note", "service_requests", "perf_report", "patient_flow",
    ]
    for r in results:
        assert r["requests"] == 4
        assert r["errors"] == 0, r
        assert r["p50_ms"] > 0 and r["p95_ms"] >= r["p50_ms"]
        assert r["qps"] > 0

    text = format_results(results)
    assert "P95" in text and "exam_order" in text


def test_benchmark_rerun_idempotent_setup(client):
    """重复执行：机构/患者准备幂等，不因已存在而失败。"""
    transport = httpx.ASGITransport(app=app)
    results = asyncio.run(
        run_benchmark(
            "http://testserver",
            concurrency=1,
            requests_per_scenario=2,
            transport=transport,
        )
    )
    assert sum(r["errors"] for r in results) == 0
