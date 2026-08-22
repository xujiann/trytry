"""CI 闸门接线守卫：真 PG 集成档必须**真的在 CI 里跑**，且失败要阻断。

背景：`tests/test_postgres_real.py` 与结构漂移棘轮的 PG 档都靠 `MEDPLAT_PG_TEST_URL`
的 skipif 开关控制——**没传连接串就整档跳过，而那一步的退出码仍是 0**。
于是"押金/结算/缴费这类聚合判定不加锁"的缺陷修完之后，兜着它们的网可以在
任何一次配置手滑里静默消失，没有人会收到通知：绿灯长得一模一样。

这条用例做的是**配置走查**（yaml.safe_load + 逻辑核对），不需要真的起一个 CI：
接线断了当场变红。它守四件事——
  1. test job 挂了 postgres service（生产同版镜像）；
  2. 有一步用 `-m integration` 跑集成档，且把 MEDPLAT_PG_TEST_URL 指向那个 service；
  3. 这一步**阻断**（job 与 step 都不许 continue-on-error）；
  4. 有一步做"集成档到底跑了几条"的自证，且在一条都没跑时阻断——
     否则第 2、3 条都还在，整档静默跳过依旧全绿（第 17 章：绿灯本身不是证据）。
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

CI_PATH = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _ci() -> dict:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def _test_job() -> dict:
    jobs = _ci()["jobs"]
    assert "test" in jobs, f"CI 里没有 test job：{sorted(jobs)}"
    return jobs["test"]


def _steps() -> list[dict]:
    return [s for s in _test_job().get("steps", []) if isinstance(s, dict)]


def _integration_step() -> dict:
    hits = [s for s in _steps() if "-m integration" in str(s.get("run", ""))]
    assert hits, (
        "CI 的 test job 里没有跑集成档的步骤（`pytest -m integration`）——"
        "真 PG 用例又变回了'默认 skip、CI 不跑'的状态"
    )
    assert len(hits) == 1, f"跑集成档的步骤有 {len(hits)} 个，接线应当只有一处"
    return hits[0]


def test_test_job挂了postgres服务容器():
    services = _test_job().get("services", {})
    assert "postgres" in services, f"test job 没挂 postgres service：{sorted(services)}"
    image = str(services["postgres"].get("image", ""))
    assert image.startswith("postgres:"), f"postgres service 镜像可疑：{image}"
    # 没有 health-check 就可能在库还没起来时开跑，表现为随机失败——比不跑更糟（会被当成 flaky 关掉）
    assert "pg_isready" in str(services["postgres"].get("options", "")), (
        "postgres service 缺 pg_isready 健康检查"
    )


def test_集成档连上的是那个服务容器():
    env = _integration_step().get("env", {})
    url = str(env.get("MEDPLAT_PG_TEST_URL", ""))
    assert url.startswith("postgresql://"), (
        f"集成档步骤没导出 MEDPLAT_PG_TEST_URL（导出的是 {env}）——"
        "不导出就是整档 skip，等于没跑"
    )
    assert "5432" in url, f"MEDPLAT_PG_TEST_URL 没指向 postgres service 的端口：{url}"


def test_集成档是阻断门而不是warning档():
    job = _test_job()
    assert not job.get("continue-on-error"), "test job 被设成 continue-on-error，集成档失败不再阻断"
    step = _integration_step()
    assert not step.get("continue-on-error"), "集成档步骤被设成 continue-on-error，失败不再阻断"


def test_集成档必须自证真的跑过():
    """光有'跑集成档'这一步还不够：整档跳过时它照样退 0。

    所以要求存在一步：读 junit 结果、打印"收集/跳过/执行"三个数字、
    并在执行数为 0 时 exit 1。这是第 17 章 §17.5 第 4 条（检查工具自证覆盖面）
    在 CI 上的落法。
    """
    assert "--junitxml" in str(_integration_step().get("run", "")), (
        "集成档步骤没产出 junit 结果，下游无法核对'到底跑了几条'"
    )
    checks = [
        s for s in _steps()
        if "integration.xml" in str(s.get("run", "")) and "sys.exit(1)" in str(s.get("run", ""))
    ]
    assert checks, (
        "缺少'集成档跑没跑'的自证步骤：整档 skip 与全部通过在退出码上不可分辨，"
        "少了这一步，MEDPLAT_PG_TEST_URL 一旦掉了就静默变回假绿"
    )
    run = str(checks[0]["run"])
    assert "ran <= 0" in run or "ran == 0" in run, "自证步骤没有在'执行 0 条'时阻断"


def test_覆盖面数字必须打印出来():
    """不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险（第 17 章例三）。"""
    run = str(
        next(
            s for s in _steps()
            if "integration.xml" in str(s.get("run", "")) and "sys.exit(1)" in str(s.get("run", ""))
        )["run"]
    )
    for token in ("收集", "跳过", "真正执行"):
        assert token in run, f"自证步骤没打印「{token}」这项数字"
