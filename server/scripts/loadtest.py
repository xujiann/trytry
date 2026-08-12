#!/usr/bin/env python3
"""压测基线工具：不依赖 locust，用 httpx + asyncio 并发压测三个核心接口。

场景：
1. login    —— POST /api/auth/login（统一认证；含口令散列验证开销）
2. patients —— GET  /api/patients?keyword=（患者检索，高频读）
3. exam_order — POST /api/exams（检查开单，高频写）
4. progress_note —— POST 住院病程记录（查房高频写）
5. service_requests — GET 统一申请单中心（跨五类单据聚合，随数据量退化最快）
6. perf_report —— GET 综合绩效报告（全机构 × 全公式，报表类最重）
7. patient_flow —— GET 就医流向（县域就诊率口径）

输出每场景 P50 / P95 / 平均耗时 / 错误数 / QPS。

用法（先起服务）：
    cd server
    uvicorn app.main:app --port 8000 &
    python scripts/loadtest.py http://127.0.0.1:8000 --concurrency 10 --requests 100

说明：
- 压测前自动用 admin 账号准备一个机构与一名患者（幂等）；
- 开单场景会产生真实数据，请勿对生产库执行；
- P95 基线建议（本地 SQLite 单实例、演示数据量）：login<300ms、patients<100ms、
  exam_order<150ms、progress_note<150ms、service_requests<200ms、
  patient_flow<200ms、perf_report<400ms。
  perf_report 与 service_requests 是最需要盯的两条——前者是全机构 × 全公式的
  报表，后者跨五类单据聚合，都随数据量增长最快。
"""
import argparse
from datetime import datetime
import asyncio
import statistics
import sys
import time

import httpx

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return ordered[idx]


async def _timed(coro_factory) -> tuple[float, bool]:
    start = time.perf_counter()
    try:
        resp = await coro_factory()
        ok = resp.status_code < 400
    except Exception:
        ok = False
    return (time.perf_counter() - start) * 1000, ok


async def _run_scenario(name, make_request, total, concurrency):
    latencies: list[float] = []
    errors = 0
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)

    async def worker():
        nonlocal errors
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            elapsed_ms, ok = await _timed(make_request)
            latencies.append(elapsed_ms)
            if not ok:
                errors += 1

    wall_start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall = time.perf_counter() - wall_start
    return {
        "scenario": name,
        "requests": total,
        "errors": errors,
        "p50_ms": round(_percentile(latencies, 50), 1),
        "p95_ms": round(_percentile(latencies, 95), 1),
        "avg_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "qps": round(total / wall, 1) if wall > 0 else 0.0,
    }


async def run_benchmark(
    base_url: str,
    concurrency: int = 10,
    requests_per_scenario: int = 50,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """执行全部场景压测并返回结果列表。transport 供测试注入 ASGITransport。"""
    async with httpx.AsyncClient(
        base_url=base_url, transport=transport, timeout=30, trust_env=False
    ) as client:
        # ---- 准备：登录 + 幂等准备机构与患者 ----
        login_resp = await client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        login_resp.raise_for_status()
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

        org_resp = await client.post(
            "/api/organizations",
            json={"name": "压测基准卫生院", "org_type": "township", "level": "township"},
            headers=headers,
        )
        if org_resp.status_code == 201:
            org_id = org_resp.json()["id"]
        else:  # 已存在：从列表反查（幂等重跑）
            orgs = (await client.get("/api/organizations", headers=headers)).json()
            org_id = next(o["id"] for o in orgs if o["name"] == "压测基准卫生院")
        patient_resp = await client.post(
            "/api/patients",
            json={"name": "压测患者", "id_card": "320981199001019999"},
            headers=headers,
        )
        patient_id = patient_resp.json()["id"]

        # 聚合类接口的前置：一条在院住院记录（供文书接口压测）
        ward_resp = await client.post(
            "/api/inpatient/wards", json={"org_id": org_id, "name": "压测病区"}, headers=headers
        )
        if ward_resp.status_code == 201:
            ward_id = ward_resp.json()["id"]
        else:
            wards = (await client.get(f"/api/inpatient/wards?org_id={org_id}", headers=headers)).json()
            ward_id = next(w["id"] for w in wards if w["name"] == "压测病区")
        admissions = (
            await client.get(f"/api/inpatient/admissions?patient_id={patient_id}", headers=headers)
        ).json()
        admitted = [a for a in admissions if a["status"] == "admitted"]
        if admitted:
            admission_id = admitted[0]["id"]
        else:
            bed = (await client.post(
                "/api/inpatient/beds",
                json={"ward_id": ward_id, "bed_no": f"LT{len(admissions):03d}"},
                headers=headers,
            )).json()
            admission_id = (await client.post(
                "/api/inpatient/admissions",
                json={"patient_id": patient_id, "ward_id": ward_id, "bed_id": bed["id"],
                      "doctor_name": "压测医师", "diagnosis_name": "压测诊断"},
                headers=headers,
            )).json()["id"]
        period = datetime.now().strftime("%Y-%m")

        # ---- 场景 ----
        results = []
        results.append(
            await _run_scenario(
                "login",
                lambda: client.post(
                    "/api/auth/login", json={"username": username, "password": password}
                ),
                requests_per_scenario,
                concurrency,
            )
        )
        results.append(
            await _run_scenario(
                "patients",
                lambda: client.get("/api/patients?keyword=压测", headers=headers),
                requests_per_scenario,
                concurrency,
            )
        )
        results.append(
            await _run_scenario(
                "exam_order",
                lambda: client.post(
                    "/api/exams",
                    json={
                        "patient_id": patient_id,
                        "from_org_id": org_id,
                        "center_type": "lab",
                        "item_code": "LOAD-CBC",
                        "item_name": "血常规(压测)",
                    },
                    headers=headers,
                ),
                requests_per_scenario,
                concurrency,
            )
        )
        # ---- 阶段一~五新增的重接口：聚合与报表类，最容易随数据量退化 ----
        results.append(
            await _run_scenario(
                "progress_note",
                lambda: client.post(
                    f"/api/inpatient/admissions/{admission_id}/progress-notes",
                    json={"note_type": "daily", "content": "压测病程记录内容"},
                    headers=headers,
                ),
                requests_per_scenario,
                concurrency,
            )
        )
        results.append(
            await _run_scenario(
                "service_requests",
                lambda: client.get(
                    f"/api/service-requests?patient_id={patient_id}", headers=headers
                ),
                requests_per_scenario,
                concurrency,
            )
        )
        results.append(
            await _run_scenario(
                "perf_report",
                lambda: client.get(
                    f"/api/analytics/performance-report?period={period}", headers=headers
                ),
                requests_per_scenario,
                concurrency,
            )
        )
        results.append(
            await _run_scenario(
                "patient_flow",
                lambda: client.get("/api/analytics/patient-flow", headers=headers),
                requests_per_scenario,
                concurrency,
            )
        )
        return results


def format_results(results: list[dict]) -> str:
    header = f"{'场景':<18}{'请求数':>8}{'错误':>6}{'P50(ms)':>10}{'P95(ms)':>10}{'均值(ms)':>10}{'QPS':>8}"
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r['scenario']:<18}{r['requests']:>8}{r['errors']:>6}"
            f"{r['p50_ms']:>10}{r['p95_ms']:>10}{r['avg_ms']:>10}{r['qps']:>8}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="核心接口压测基线（含阶段一~五新增的聚合与报表接口）")
    parser.add_argument("base_url", help="服务地址，如 http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=10, help="并发数（默认10）")
    parser.add_argument("--requests", type=int, default=50, help="每场景请求数（默认50）")
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    results = asyncio.run(
        run_benchmark(
            args.base_url,
            concurrency=args.concurrency,
            requests_per_scenario=args.requests,
            username=args.username,
            password=args.password,
        )
    )
    print(format_results(results))
    total_errors = sum(r["errors"] for r in results)
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
