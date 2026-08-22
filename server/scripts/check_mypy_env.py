"""校验 mypy 当前环境**能解析到第三方库的类型**，否则拒绝把结果当数。

为什么需要这个：`pyproject.toml` 里开了 `ignore_missing_imports = true`
（第三方库多无 stub，不开会淹在噪音里）。代价是——当 mypy **找不到**某个库时，
它不报错，而是把这个库的一切静静当成 `Any`。于是依赖它的代码全部"通过"。

这不是假设。本仓库真踩过：某开发机的 `mypy` 是 `uv tool install` 装的**隔离环境**，
里面有 mypy 但没有本项目的依赖，于是 SQLAlchemy 整个被当成 Any——`mypy app` 报 **41** 处，
同一份代码在 CI（mypy 与依赖装在同一环境）里是 **187** 处。差的 146 处全是 ORM 相关。
本地那份「绿」只是因为检查器瞎了；照着本地数字去决定「能不能转阻断」，
会把一个根本没在工作的门当成已经通过。

所以在跑 mypy 之前先探一下：让它对 `Session.query` 做 `reveal_type`，
若结果是 `Any`，说明 SQLAlchemy 的类型没被解析到，直接失败并说明怎么修。

第二道探针是**版本**：`requirements-dev.txt` 把 mypy 钉在一个小版本区间里，
因为大版本间推断差异极大（2.3 报 0 处，1.19 在同一份代码上报 138 处）。
装了区间外的版本，本地和 CI 检查的就不是同一件事——这同样是「假绿」，
只不过瞎的不是库解析而是版本。踩过一次：本地 1.19 全绿，CI 的 2.3
报出 `is_suspect_risk` 收到 `Any | object`。
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: 探针：这些库有 py.typed，正常环境下 reveal_type 必须给出具体类型而非 Any。
PROBES = [
    ("sqlalchemy", "from sqlalchemy.orm import Session\nreveal_type(Session.query)\n"),
    ("pydantic", "from pydantic import BaseModel\nreveal_type(BaseModel.model_validate)\n"),
]


#: 依赖清单的唯一真源——版本区间只写在 requirements-dev.txt，这里读它，不复制一份。
REQUIREMENTS_DEV = Path(__file__).resolve().parent.parent / "requirements-dev.txt"


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 "2.3.1" / "2.3" 变成可比较的整数元组（后缀如 rc1 直接截掉）。"""
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _pinned_range() -> tuple[str, tuple[int, ...] | None, tuple[int, ...] | None]:
    """从 requirements-dev.txt 里读 mypy 的下界/上界，读不到就返回空区间（不拦）。"""
    spec = ""
    for line in REQUIREMENTS_DEV.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line.lower().startswith("mypy"):
            spec = line
            break
    low = high = None
    for op, ver in re.findall(r"(>=|<=|<|==)\s*([0-9][0-9.]*)", spec):
        if op in (">=", "=="):
            low = _version_tuple(ver)
        elif op in ("<", "<="):
            high = _version_tuple(ver)
    return spec, low, high


def _check_version(mypy_bin: str) -> str:
    """版本落在钉住的区间外就返回一段说明，落在区间内返回空串。"""
    spec, low, high = _pinned_range()
    if low is None and high is None:
        return ""
    out = subprocess.run([mypy_bin, "--version"], capture_output=True, text=True).stdout
    found = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    if found is None:
        return ""
    got = _version_tuple(found.group(1))
    if (low is not None and got < low) or (high is not None and got >= high):
        return (
            f"错误：mypy 版本 {found.group(1)} 不在 requirements-dev.txt 钉住的区间里"
            f"（{spec}），本次检查结果与 CI 不可比。\n\n"
            "大版本间推断差异极大，装错版本的「本地全绿」照样会在 CI 变红。\n\n"
            f"当前用的 mypy：{mypy_bin}\n\n"
            "怎么修：\n"
            "    pip install -r server/requirements.txt -r server/requirements-dev.txt\n"
        )
    return ""


def main() -> int:
    mypy_bin = shutil.which("mypy")
    if mypy_bin is None:
        print("错误：PATH 上找不到 mypy，请先 pip install -r requirements-dev.txt", file=sys.stderr)
        return 1
    version_problem = _check_version(mypy_bin)
    if version_problem:
        print(version_problem, file=sys.stderr)
        return 1
    blind = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, source in PROBES:
            probe = Path(tmp) / f"probe_{name}.py"
            probe.write_text(source, encoding="utf-8")
            # 必须探**与 `make typecheck` 同一个** mypy：隔离安装（uv tool / pipx）
            # 的可执行文件和 `python -m mypy` 可能是两套环境，一个瞎一个不瞎。
            out = subprocess.run(
                [mypy_bin, "--no-incremental", "--cache-dir",
                 str(Path(tmp) / "cache"), str(probe)],
                capture_output=True, text=True,
            ).stdout
            # mypy 把 reveal_type 的结果写成 `note: Revealed type is "..."`
            revealed = [ln for ln in out.splitlines() if "Revealed type" in ln]
            if not revealed or all('is "Any"' in ln for ln in revealed):
                blind.append(name)

    if not blind:
        return 0
    print(
        "错误：mypy 解析不到以下库的类型，本次检查结果不可信：\n"
        f"    {', '.join(blind)}\n\n"
        "因为 pyproject.toml 开了 ignore_missing_imports=true，解析不到的库会被\n"
        "静默当成 Any——依赖它的代码一律「通过」，报出的错误数会远小于真实值\n"
        "（本仓库实测：本地 41 处 vs CI 187 处，差的全是 ORM 相关）。\n\n"
        f"当前用的 mypy：{mypy_bin}\n\n"
        "怎么修：让 mypy 和项目依赖装在**同一个**环境里——\n"
        "    python -m venv .venv && . .venv/bin/activate\n"
        "    pip install -r server/requirements.txt -r server/requirements-dev.txt\n\n"
        "最常见的来源是隔离安装：`uv tool install mypy` / `pipx install mypy` 会把\n"
        "mypy 放进自己的环境，那里没有 SQLAlchemy，于是 ORM 全成了 Any。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
