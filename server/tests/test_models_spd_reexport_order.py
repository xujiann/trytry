"""先 import `app.spd.models`、后 import `app.models`，平台 models 照样认得全部子系统模型（测试隔离）。

`app/models/__init__.py` 末尾星号导入子系统模型，而子系统模型自己又 `from ..models import Money, utcnow`。先 import 子系统
模型的进程里，平台 models 是被那一行拉起来的——星号导入执行时子系统模型才初始化到那一行，拿到的是半个模块，平台 models
上一个 `Spd*` 都没有。应用自己总是先 import `app.models`，碰不上；测试碰得上：单跑
`test_spd_package_period_follows_start.py test_spd_enrollment_org_guard.py` 时，后者全部报
「module 'app.models' has no attribute 'SpdProgram'」——报在与肇事文件毫不相干的用例上。

只有新起的解释器才看得到先后顺序，所以用子进程跑。
"""
import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]

PROBE = """
import app.database
import app.spd.models
import app.models as M
from app.models import SpdEnrollment
assert M.SpdProgram is app.spd.models.SpdProgram
assert SpdEnrollment is app.spd.models.SpdEnrollment
try:
    M.NoSuchModel
except AttributeError as exc:
    assert "has no attribute 'NoSuchModel'" in str(exc), exc
else:
    raise AssertionError("不存在的名字应当照旧 AttributeError")
print("ok")
"""


def test_先import子系统模型_平台models照样认得全部Spd模型(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SERVER), "MEDPLAT_DATABASE_URL": "sqlite:///./probe.db"}
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-2000:]
