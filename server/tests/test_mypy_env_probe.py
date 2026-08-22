"""mypy 环境探针（`scripts/check_mypy_env.py`）自身的回归网。

探针的职责是拦住「假绿」——本地检查结果与 CI 不可比时，宁可红也不放行。
它有两道：库解析（解析不到 SQLAlchemy 就整包当 Any）与**版本**。
第二道是补进来的：本地装了 1.19、CI 装 requirements-dev 钉住的 2.3，
同一份代码本地全绿而 CI 报错——那次红就是这么来的。

这里钉住的是探针的判定逻辑本身：区间从 requirements-dev.txt 读（不复制第二份），
区间外的版本必须非零退出并说清怎么修。
"""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_mypy_env  # noqa: E402


def _fake_mypy(tmp_path: Path, version: str) -> str:
    """造一个只会打印版本号的假 mypy，用来喂给探针的版本判定。"""
    fake = tmp_path / "mypy"
    fake.write_text(f'#!/bin/sh\necho "mypy {version} (compiled: yes)"\n', encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_区间从依赖清单读而不是写死在探针里():
    spec, low, high = check_mypy_env._pinned_range()
    assert spec.lower().startswith("mypy"), "没在 requirements-dev.txt 里找到 mypy 行"
    assert low is not None and high is not None, f"mypy 的上下界没钉全：{spec}"
    assert low < high
    # 真源就是那份清单：改了清单，探针跟着变，不需要同步改代码
    text = (SCRIPTS.parent / "requirements-dev.txt").read_text(encoding="utf-8")
    assert ".".join(str(p) for p in low) in text


def test_区间内的版本放行(tmp_path):
    _, low, _ = check_mypy_env._pinned_range()
    inside = ".".join(str(p) for p in low)
    assert check_mypy_env._check_version(_fake_mypy(tmp_path, inside)) == ""


def test_低于下界的版本被拦下并给出修法(tmp_path):
    _, low, _ = check_mypy_env._pinned_range()
    older = f"{low[0] - 1}.0.0" if low[0] > 1 else f"{low[0]}.{max(low[1] - 1, 0)}.0"
    msg = check_mypy_env._check_version(_fake_mypy(tmp_path, older))
    assert older in msg and "requirements-dev.txt" in msg


def test_到达上界的版本被拦下(tmp_path):
    _, _, high = check_mypy_env._pinned_range()
    at_high = ".".join(str(p) for p in high)  # 上界是 `<`，等于上界即出界
    assert check_mypy_env._check_version(_fake_mypy(tmp_path, at_high)) != ""


def test_探针整体在出界版本上非零退出(tmp_path):
    """判定逻辑绿不算数，得让 `make typecheck` 真的停下来。"""
    _, low, _ = check_mypy_env._pinned_range()
    older = f"{max(low[0] - 1, 0)}.0.0"
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    _fake_mypy(fake_dir, older)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_mypy_env.py")],
        capture_output=True, text=True,
        env={"PATH": f"{fake_dir}:/usr/bin:/bin"},
    )
    assert proc.returncode == 1
    assert "不在 requirements-dev.txt 钉住的区间里" in proc.stderr
