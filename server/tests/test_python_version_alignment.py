"""守卫：CI、生产镜像、演示环境、工具配置必须用**同一个** Python 版本。

修的问题：CI 两个 job 装 3.11，而生产镜像是 `python:3.12-slim`，
`pyproject.toml` 里 ruff `target-version` / mypy `python_version` 也是 3.12。
后果有两层——

1. **测的和发的不是同一个解释器**：CI 全绿不代表生产跑得起来；
2. 两个阻断门口径不一：lint/typecheck 按 3.12 语义检查、测试按 3.11 语义执行，
   3.12 才有的写法能过 quality、却在 test 里炸（反之亦然）。

这几处没有任何机制互相约束，正是它们悄悄漂开的原因。这里把它们钉在一起：
以后升级 Python 要么一起改，要么这两条用例变红。

（`.devcontainer/devcontainer.json` 是后来补进来的第五处——在线演示环境同样是
"跑我们代码的解释器"，理由见 `_devcontainer_version`。）
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = ROOT / "server"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} 不存在，测试需要跟着改"
    return path.read_text(encoding="utf-8")


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _ci_python_version() -> str:
    text = _read(ROOT / ".github" / "workflows" / "ci.yml")
    match = re.search(r'^\s*PYTHON_VERSION:\s*["\']?([\d.]+)["\']?', text, re.M)
    assert match, "ci.yml 里找不到 PYTHON_VERSION"
    return match.group(1)


def _dockerfile_versions() -> dict[str, str]:
    out = {}
    for path in (ROOT / "Dockerfile", SERVER / "Dockerfile"):
        match = re.search(r"^FROM python:([\d.]+)", _read(path), re.M)
        assert match, f"{path} 的 FROM 不是 python:<版本>"
        out[str(path.relative_to(ROOT))] = match.group(1)
    return out


def _devcontainer_version() -> dict[str, str]:
    """在线演示（Codespaces）用的镜像同样是"跑我们这份代码的解释器"。

    它和 CI/生产镜像漂开的后果与别处同构：演示站跑在另一个版本上，
    "在 Codespaces 里能跑起来"就不再说明生产跑得起来——而演示恰恰是
    最常被拿来当"证明它能跑"的那个环境。
    """
    path = ROOT / ".devcontainer" / "devcontainer.json"
    match = re.search(r'"image"\s*:\s*"[^"]*python:([\d.]+)', _read(path))
    assert match, f"{path} 的 image 不是 .../python:<版本>-… 形态"
    return {".devcontainer/devcontainer.json": match.group(1)}


def _pyproject_versions() -> dict[str, str]:
    text = _read(SERVER / "pyproject.toml")
    ruff = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', text)
    mypy = re.search(r'python_version\s*=\s*"([\d.]+)"', text)
    assert ruff and mypy, "pyproject.toml 里找不到 ruff target-version 或 mypy python_version"
    return {
        "ruff target-version": f"{ruff.group(1)}.{ruff.group(2)}",
        "mypy python_version": mypy.group(1),
    }


def test_ci_生产镜像_工具配置同版本():
    ci = _ci_python_version()
    everything = {
        "ci.yml PYTHON_VERSION": ci,
        **_dockerfile_versions(),
        **_devcontainer_version(),
        **_pyproject_versions(),
    }
    mismatched = {k: v for k, v in everything.items() if v != ci}
    assert not mismatched, (
        f"Python 版本不一致：CI 是 {ci}，但 {mismatched} 不同。\n"
        "CI 与生产镜像不同版 = 测的和发的不是同一个解释器；\n"
        "工具配置与 CI 不同版 = lint/typecheck 与测试两个阻断门口径不一。\n"
        "升级 Python 请五处一起改（CI / 两个 Dockerfile / devcontainer / pyproject），"
        "并先在目标版本上跑通 make verify。"
    )


def test_所有workflow都引用变量而不是写死版本():
    """写死版本号就等于给漂移留了口子——各 job 各写各的，迟早不一样。

    扫**全部** workflow 文件而不只是 ci.yml（新加一个 workflow 同样能把版本写歪），
    且版本号带不带引号都要认——YAML 里 `python-version: 3.11` 是合法写法，
    只匹配带引号的等于给漂移留了后门。
    """
    assert WORKFLOWS, "找不到任何 workflow 文件，测试需要跟着改"
    hardcoded = []
    for path in WORKFLOWS:
        for line in re.findall(r'^\s*python-version:.*$', _read(path), re.M):
            if "env.PYTHON_VERSION" not in line:
                hardcoded.append(f"{path.name}:{line.strip()}")
    assert not hardcoded, (
        f"workflow 里有写死的 python-version：{hardcoded}；"
        "应统一引用 ${{ env.PYTHON_VERSION }}"
    )
