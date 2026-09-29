"""静态前端脚本逐个过 `node --check`：一处语法错误让整份脚本不加载，单元档原先看不出来。

前端免构建，`app/static/**/*.js` 原样下发给浏览器。一份脚本有语法错误，浏览器整份不执行——里面的页面函数全部未定义，
SPA 起不来。第一百二十轮验证实测：P2-852 在非 async 的点击处理里写了 `await spdModal(...)`（`pages-public.js`），单元档
全绿（页面闸门都是按文本 grep 的，看不出 `await` 放错了地方），端到端档一条不剩全红，而那一档在 CI 上要等推上去半小时后
才知道。这里在单元档逐个 `node --check`（按经典脚本解析，与浏览器 `<script>` 同一个语法）。本机没有 node 的跳过，与
`test_error_text_too_long` 同一写法；CI 的 ubuntu 镜像自带 node。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SCRIPTS = sorted(STATIC.rglob("*.js"))


def test_找得到这些脚本():
    assert {"core.js", "pages-public.js", "pages-spd.js"} <= {p.name for p in SCRIPTS}   # 路径写错时别悄悄什么都不查


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可做语法检查")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(STATIC)))
def test_静态脚本语法正确(script):
    got = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
