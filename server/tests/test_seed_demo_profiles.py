"""两种文档化的演示档都灌不进演示数据（P2-1278，第三十七批「种子与初始化数据」扫描 AA4-5）。

① Render 档：`render.yaml` 开 MEDPLAT_SEED_DEMO=1，管理员口令要求在控制台手填强口令（P0-1 的修法），而
`scripts/seed_demo.py` 写死用 admin/admin123 登录——修前实测第一个请求就 401、拿 401 的响应体取 access_token 抛
KeyError，被 `start.sh` 的 `|| true` 吞得一行不剩：机构 0、患者 0、就诊 0。要灌进去只能把口令设回 admin123，也就是
P0-1 修掉的公网默认口令。
② compose 档：`docker-compose.yml` 写死 MEDPLAT_ENV: prod，注释却教「要起演示档需显式开 export MEDPLAT_SEED_DEMO=1 再
up」——照做则生产守卫拒启、`set -e` 退出、`restart: unless-stopped` 无限重启（P0-2 修过的同形崩溃循环）。

修后 seed_demo 的口令与 lifespan 建 admin 同一个来源（`settings.admin_password`，即 MEDPLAT_ADMIN_PASSWORD，没设才是缺省
口令），登录不上说清原因、非零退出；start.sh 仍不让灌数失败拖垮服务，但失败时往 stderr 打一行「演示数据灌入失败：……」；
compose 注释改为写明本文件是生产档、演示走 render.yaml 或本地开发。进程内跑法同 `test_seed_demo_rerun.py`。
"""
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import Settings, settings
from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Organization, Patient

ROOT = Path(__file__).resolve().parents[2]
SEED_DEMO = ROOT / "server" / "scripts" / "seed_demo.py"
START_SH = ROOT / "server" / "start.sh"

#: 测试用的假强口令（render.yaml 要求 ≥12 位强口令；不是任何环境的真实口令）
FAKE_STRONG = "Fake-Demo#Pass-2026"


@pytest.fixture()
def responses(monkeypatch):
    """脚本里每个 `httpx.Client(...)` 换成对着应用的 TestClient，逐个记下（方法、路径、状态码）。"""
    seen: list[tuple[str, str, int]] = []

    def _client(*args, **kwargs):
        client = TestClient(app, raise_server_exceptions=False)
        client.event_hooks["response"].append(
            lambda r: seen.append((r.request.method, r.request.url.path, r.status_code)))
        return client

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", "http://testserver"])
    return seen


def _boot_with_admin_password(monkeypatch, password: str) -> None:
    """空库按部署口令起一次：lifespan 用 settings.admin_password 建 admin（与 MEDPLAT_ADMIN_PASSWORD 同源）。"""
    monkeypatch.setattr(settings, "admin_password", password)
    reset_database()
    with TestClient(app):
        pass


def _counts() -> tuple[int, int, int]:
    with SessionLocal() as db:
        return db.query(Organization).count(), db.query(Patient).count(), db.query(Encounter).count()


def test_前提_admin口令的来源就是MEDPLAT_ADMIN_PASSWORD(monkeypatch):
    monkeypatch.setenv("MEDPLAT_ADMIN_PASSWORD", FAKE_STRONG)
    assert Settings().admin_password == FAKE_STRONG


def test_部署配了强口令_演示种子照常灌入(monkeypatch, capsys, responses):
    _boot_with_admin_password(monkeypatch, FAKE_STRONG)
    runpy.run_path(str(SEED_DEMO), run_name="__main__")   # 修前：KeyError 'access_token'
    assert responses[0] == ("POST", "/api/auth/login", 200)
    assert [r for r in responses if r[2] >= 400] == []
    assert all(_counts()), _counts()
    assert "末端自检通过" in capsys.readouterr().out


def test_口令对不上_非零退出并说清原因_一条不灌(monkeypatch, responses):
    _boot_with_admin_password(monkeypatch, FAKE_STRONG)
    monkeypatch.setattr(settings, "admin_password", "Another#Wrong-2026")   # 脚本这边的口令与建 admin 时的不一致
    with pytest.raises(SystemExit) as exc:   # 修前抛的是 KeyError，不是带原因的退出
        runpy.run_path(str(SEED_DEMO), run_name="__main__")
    message = exc.value.code
    # SystemExit 带的是字符串：进程退出码 1、这句打到 stderr
    assert isinstance(message, str), message
    assert "演示数据灌入失败" in message and "admin 登录被拒" in message and "401" in message, message
    assert "MEDPLAT_ADMIN_PASSWORD" in message, message
    assert FAKE_STRONG not in message and "Another#Wrong-2026" not in message, "口令不该打进输出"
    assert responses == [("POST", "/api/auth/login", 401)]
    assert _counts() == (0, 0, 0)


# ---------------------------------------------------------------- start.sh：失败不拖垮服务，但要说出来

#: 假的 uvicorn：起来就退（演示档走后台 + wait，退了 wait 就返回）
FAKE_UVICORN = "#!/bin/sh\nexit 0\n"
#: 假的 python：健康检查（-c）一律通过；灌数脚本按 $FAKE_SEED_RC 退出，先往 stderr 写一句它自己的原因
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "scripts/seed_demo.py" ]; then
  [ "$FAKE_SEED_RC" = "0" ] || echo "假灌数脚本：admin 登录被拒" >&2
  exit "$FAKE_SEED_RC"
fi
exit 99
"""


def _run_demo_start_sh(tmp_path: Path, seed_rc: int) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name, body in (("uvicorn", FAKE_UVICORN), ("python", FAKE_PYTHON)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("MEDPLAT_WORKERS", "WEB_CONCURRENCY")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", MEDPLAT_MIGRATE_ON_START="0",
               MEDPLAT_SEED_DEMO="1", FAKE_SEED_RC=str(seed_rc))
    return subprocess.run(["sh", str(START_SH)], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)


def test_start_sh灌数失败_打一行明确原因到stderr_服务照常(tmp_path):
    proc = _run_demo_start_sh(tmp_path, seed_rc=1)
    assert proc.returncode == 0, proc.stderr   # 没被灌数失败带走：照常走到 wait uvicorn
    assert "假灌数脚本：admin 登录被拒" in proc.stderr   # 防空转：确实跑到了灌数那一步
    # 修前这里一行都没有：失败被 `|| true` 吞掉
    assert re.search(r"演示数据灌入失败：scripts/seed_demo\.py 退出码 1", proc.stderr), proc.stderr


def test_start_sh灌数成功_不打失败行(tmp_path):
    proc = _run_demo_start_sh(tmp_path, seed_rc=0)
    assert proc.returncode == 0, proc.stderr
    assert "演示数据灌入失败" not in proc.stderr, proc.stderr


# ---------------------------------------------------------------- docker-compose.yml：生产档不教人开演示种子


def _comment_above(lines: list[str], index: int) -> str:
    block = []
    while index > 0 and lines[index - 1].lstrip().startswith("#"):
        index -= 1
        block.insert(0, lines[index])
    return "\n".join(block)


def test_compose生产档不再教人开演示种子():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    lines = compose.splitlines()
    # 前提：本文件写死生产档——生产守卫见到演示种子开着就拒启（config.py，test_ops_prod_guard 盯着）
    assert any(re.fullmatch(r"\s*MEDPLAT_ENV:\s*prod\s*", line) for line in lines)
    # 修前注释：「要起演示档需显式开：export MEDPLAT_SEED_DEMO=1 再 up」——照做即拒启、无限重启
    assert not re.search(r"MEDPLAT_SEED_DEMO\s*=\s*1", compose)
    seed_line = next(i for i, line in enumerate(lines) if re.match(r"\s*MEDPLAT_SEED_DEMO:", line))
    note = _comment_above(lines, seed_line)
    assert "生产档" in note and "render.yaml" in note, note
