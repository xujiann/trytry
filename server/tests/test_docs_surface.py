"""API 文档面的环境开关：生产关闭 /docs /redoc /openapi.json，开发保留。

为什么要关：这三个路由把 881 个端点的完整结构（路径、入参形状、鉴权盲区）
展示给任何能连到服务的人，等于给越权探测发一张现成地图。文档在开发/演示
环境照常可用；附录与契约测试用的是进程内 `app.openapi()`（如
tests/test_api.py），不走这三个 HTTP 路由，不受影响。

app 是模块级单例、settings 在 import 时定值，所以生产形态必须在**子进程**里
用生产环境变量重新 import 才测得真——monkeypatch settings 改不动已构造好的 app。
"""
import os
import subprocess
import sys

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..")

#: 子进程脚本：不进 `with TestClient(...)`，不触发 lifespan——
#: 生产配置指向的 PostgreSQL 在测试机上并不存在，起 lifespan 会去连库。
_PROBE = """
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
print(c.get("/docs").status_code, c.get("/redoc").status_code, c.get("/openapi.json").status_code)
"""

# 与 tests/test_ops_prod_guard.py 同一套"能过凭据强度校验"的生产参数
_PROD_ENV = {
    "MEDPLAT_ENV": "prod",
    "MEDPLAT_SECRET": "9f3c2b7e" * 4 + "a1d4",
    "MEDPLAT_ADMIN_PASSWORD": "Kx7!mQ2$vLp9",
    "MEDPLAT_DATABASE_URL": "postgresql://medplat:pw@db:5432/medplat",
}


def _status_codes(overrides: dict[str, str]) -> list[str]:
    # 先清掉宿主/conftest 残留的 MEDPLAT_*，再注入目标形态——
    # conftest 设的 sqlite DATABASE_URL 残留会让"生产"形态被 SQLite 守卫拒启
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEDPLAT_")}
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"子进程 import app 失败：{proc.stderr[-2000:]}"
    return proc.stdout.split()[-3:]


def test_生产环境文档面全部404():
    assert _status_codes(_PROD_ENV) == ["404", "404", "404"], \
        "生产环境 /docs /redoc /openapi.json 应全部关闭"


def test_开发环境文档面照常():
    """反向断言：开关只收生产。开发/演示环境的 /docs 是日常联调入口，误关会被骂。"""
    codes = _status_codes({"MEDPLAT_ENV": "dev",
                           "MEDPLAT_DATABASE_URL": "sqlite:///./test_docs_probe.db"})
    assert codes == ["200", "200", "200"], f"开发环境文档面被误关：{codes}"
