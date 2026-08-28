"""依赖锁定纪律（P2-29 / ADR-0017）：锁必须真的锁着，且装的必须是锁。

背景（登记在 TECH_DEBT P2-29 的实测事故形态）：`redis>=5.0` 区间下，
redis-py 5.0.0 的 `socket_timeout` 默认 None（出网永久挂起）、8.1.0 默认 5 秒
——同一份代码在两次构建里装出**两种行为**，而那条路在每个请求的主路径上。
lockfile 的意义就是把"装到什么"从时间的函数变成仓库的函数。

锁会腐化的三种方式，各钉一条：
1. **锁漏项**：requirements.txt 加了新依赖、忘了再生成 lock——镜像装 lock，
   新依赖压根不进生产镜像（CI 装 lock 也会立刻炸，但要在这里给出人话诊断）；
2. **锁松动**：lock 里混进 `>=`/无版本行——"锁"退化回区间声明本身；
3. **旁路安装**：Dockerfile/CI 改回 `-r requirements.txt`——锁还在，只是
   没人用了，腐化得悄无声息（这条最阴：本地/CI 都绿，只有生产漂移）。

再生成方式见 requirements.lock 文件头注释（干净 venv freeze，含传递依赖）。
"""
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
ROOT = SERVER.parent

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _canon(name: str) -> str:
    """PEP 503 归一：大小写不敏感、`-`/`_`/`.` 等价。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_lines(path: Path) -> list[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_直接依赖必须全部入锁():
    """requirements.txt 每一项都要在 lock 里有 `==` 钉版行——加依赖必须同步再生成锁。"""
    direct = {_canon(_NAME_RE.match(ln).group(1)) for ln in _req_lines(SERVER / "requirements.txt")}
    locked = {
        _canon(ln.split("==")[0].split("[")[0])
        for ln in _req_lines(SERVER / "requirements.lock")
        if "==" in ln
    }
    missing = direct - locked
    assert not missing, (
        f"以下直接依赖不在 requirements.lock 里：{sorted(missing)}。"
        f"镜像与 CI 只装 lock——漏锁的依赖不会进生产。按 lock 文件头注释再生成。"
    )


def test_锁内必须全是钉版行():
    """lock 里出现区间（>=/~=/无版本）就不再是锁。`==` 后必须是具体版本号。"""
    for ln in _req_lines(SERVER / "requirements.lock"):
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==[A-Za-z0-9.+!*]+", ln), (
            f"requirements.lock 里的非钉版行：{ln!r}——锁必须逐项 `名==版本`"
        )


def test_镜像与CI必须装锁而不是区间():
    """四个安装点全部走 lock；改回 `-r requirements.txt` 是静默旁路（本地照绿）。"""
    installs = {
        "server/Dockerfile": SERVER / "Dockerfile",
        "根 Dockerfile": ROOT / "Dockerfile",
        "CI build.yml": ROOT / ".github" / "workflows" / "build.yml",
        "CI ci.yml": ROOT / ".github" / "workflows" / "ci.yml",
    }
    for label, path in installs.items():
        text = path.read_text(encoding="utf-8")
        assert "requirements.lock" in text, f"{label} 没有安装 requirements.lock"
        # pip install -r requirements.txt（裸区间安装）不得出现；
        # 允许的形态只有装 lock（requirements.txt 仅作为解析来源被 COPY 进去）
        bare = re.findall(r"install[^\n]*-r\s+(?:server/)?requirements\.txt", text)
        assert not bare, f"{label} 在绕开锁直接装区间声明：{bare}"
