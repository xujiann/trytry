"""容器生命周期与构建上下文的两道守卫（上线前审计）。

两个缺陷都不在应用代码里，所以既有的任何测试都碰不到它们——但它们各自
每天都在生效：一个让**每次发布都硬切在途请求**，一个让**开发机上的密钥
可能被烧进镜像**。

**① `start.sh` 不转发 SIGTERM。**
容器里 PID 1 就是这个 shell。原先它把 uvicorn 放后台再 `wait`，既没 `exec`
也没 `trap`：`docker stop` 的 SIGTERM 打在 shell 上，shell 直接退出，
PID 1 一退，内核把命名空间里剩下的进程**全部 SIGKILL**。
实测（复刻旧结构后向 shell 发 TERM）：子进程被孤儿化到 PID 1，**从没收到 TERM**。
后果是在途请求全被硬切，`lifespan` 的收尾（取消调度任务）永不执行。

**② 两个构建上下文都没有 `.dockerignore`。**
两个 Dockerfile 都是 `COPY . .` / `COPY server/ .`。最要紧的是 `.env`——
`.gitignore` 里自己写着"里面是真实密钥，绝不入库"，而 `app/config.py` 的
`env_file=".env"` 会在**运行时真的去读它**：环境变量优先，所以被显式注入的键
没事，**没被注入的键会静默取到开发值**。CI 构建的镜像是干净的（fresh checkout），
**本地/compose 构建的不是**——差别出在"谁构建"，所以更难被发现。
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = ROOT / "server"
START_SH = SERVER / "start.sh"


# ------------------------------------------------------------------ start.sh


def test_start_sh_语法通过():
    """最低线。这个脚本没有任何其他自动化覆盖，改坏了要到部署时才知道。"""
    r = subprocess.run(["sh", "-n", str(START_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_不灌演示数据时必须exec让uvicorn自己当pid1():
    """生产形态走的就是这条路，它必须是**最可靠**的那种：没有中间层。

    `exec` 比 trap 转发更可靠——trap 只在 shell 处于可中断状态时才跑得到。
    """
    src = START_SH.read_text(encoding="utf-8")
    assert re.search(r'if \[ "\$MEDPLAT_SEED_DEMO" != "1" \]; then\s*\n\s*exec "\$@"', src), (
        "非演示路径没有 exec——uvicorn 不是 PID 1，SIGTERM 到不了它"
    )


def test_演示路径必须转发信号并等子进程退干净():
    """这条路要在服务起来之后再灌数据，没法 exec，只能老实转发。"""
    src = START_SH.read_text(encoding="utf-8")
    assert "trap 'kill -TERM \"$UV_PID\" 2>/dev/null' TERM INT" in src, "没有转发 SIGTERM"
    # 第一次 wait 会被信号打断并返回 128+signo，那时子进程还在收尾；
    # 摘掉 trap 再 wait 一次才是真的等它退干净。少了这一步等于没等。
    assert src.count('wait "$UV_PID"') >= 2, "只 wait 了一次——信号打断后子进程还在收尾"
    assert "trap - TERM INT" in src, "没有在二次 wait 前摘掉 trap"


def test_守卫本身没瞎():
    """防空转：断言的前提是这个脚本真的在后台起 uvicorn。

    哪天它改成别的启动方式，上面几条会在一段不相干的文本上恒绿。
    """
    src = START_SH.read_text(encoding="utf-8")
    assert "uvicorn app.main:app" in src
    assert "UV_PID=$!" in src


# ------------------------------------------------------------- .dockerignore

#: (构建上下文目录, 对应的 Dockerfile)。两份 `.dockerignore` 的路径基准不同
#: （一份相对 `server/`、一份相对仓库根），所以不能互相复制。
CONTEXTS = [
    pytest.param(ROOT, ROOT / "Dockerfile", id="repo-root"),
    pytest.param(SERVER, SERVER / "Dockerfile", id="server"),
]


@pytest.mark.parametrize("context,dockerfile", CONTEXTS)
def test_每个构建上下文都要有dockerignore(context, dockerfile):
    assert dockerfile.exists(), f"{dockerfile} 不存在，本用例的前提不成立"
    assert (context / ".dockerignore").exists(), (
        f"{context} 是构建上下文却没有 .dockerignore——`COPY` 会把开发机上的"
        f"一切烧进镜像"
    )


@pytest.mark.parametrize("context,dockerfile", CONTEXTS)
def test_dockerignore必须挡住密钥与测试产物(context, dockerfile):
    """`.env` 是这里唯一真正危险的一条：它会被运行时**真的读取**。

    其余几项（测试库/附件/缓存）只是让镜像变大变脏，但同一份清单一起写。
    """
    text = (context / ".dockerignore").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}
    prefix = "" if context == SERVER else "server/"
    for needed in (f"{prefix}.env", f"{prefix}test_run.db*", f"{prefix}test_uploads/"):
        assert needed in lines, f"{context.name}/.dockerignore 少了 {needed}"
    assert "__pycache__/" in lines


@pytest.mark.parametrize("context,dockerfile", CONTEXTS)
def test_不得把要用的文件也挡掉(context, dockerfile):
    """防误伤：`.dockerignore` 写宽一格，构建会在 `COPY` 那一步失败或缺文件。

    两个 Dockerfile 都显式 COPY 了 requirements——把它们挡掉是最容易犯的错。
    """
    lines = {
        ln.strip()
        for ln in (context / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    for must_ship in ("requirements.txt", "requirements.lock", "app/", "start.sh", "alembic/"):
        assert must_ship not in lines, f"{must_ship} 被 .dockerignore 挡掉了，镜像会缺文件"


def test_dockerignore的路径基准与各自的构建上下文一致():
    """两份文件的路径基准不同，互相复制粘贴会让其中一份**整份失效**。

    根目录那份必须写 `server/.env`（相对仓库根），server 那份必须写 `.env`。
    写反了不会报错，只会一条都匹配不上。
    """
    root_lines = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    server_lines = (SERVER / ".dockerignore").read_text(encoding="utf-8")
    assert "server/.env" in root_lines, "根目录那份没有用 server/ 前缀，整份失效"
    assert re.search(r"^\.env$", server_lines, re.M), "server 那份不该带 server/ 前缀"
    assert "server/test_run.db*" in root_lines
