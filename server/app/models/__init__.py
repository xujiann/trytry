"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）

原本是单文件 3989 行 / 187 个模型类，按业务域拆成了包（ADR-0008）。

**导入路径不变**：各处仍然 `from ..models import User, Patient, ...`，
拆包对调用方完全透明——这是 ADR-0008 选方案 B 而不是"改上千处 import"的全部理由。

`import *` 在这里是**刻意**的：本包的职责就是把全部模型摆进一个命名空间供全仓库取用，
逐个列名字既啰嗦又必然漏（漏一个不会报错，只会让那张表不进 `Base.metadata`、
建库时静默少一张）。`tests/test_refactor_drift_guards.py` 的 246 个类名快照负责兜住这件事。
"""
from ._base import Money, utcnow  # noqa: F401 - 全仓库与子系统都从 app.models 取这两个

from .core import *  # noqa: F403
from .clinical import *  # noqa: F403
from .emergency import *  # noqa: F403
from .pharmacy import *  # noqa: F403
from .inpatient import *  # noqa: F403
from .chronic import *  # noqa: F403
from .publichealth import *  # noqa: F403
from .contracts import *  # noqa: F403
from .finance import *  # noqa: F403
from .assets import *  # noqa: F403
from .hr import *  # noqa: F403
from .quality import *  # noqa: F403
from .platform import *  # noqa: F403
from .portal import *  # noqa: F403
from .consent import *  # noqa: F403

# ============================================================================
# 全域慢专病全流程管理系统（`spd_*` 共 50 张表）
#
# 独立成 `app/spd/` 子系统包，但在这里 import 进来：`Base.metadata` 必须在
# `create_all` 之前认识这些表，而各处（测试 conftest、alembic env、应用启动）
# 导入的都是 `app.models`。放在**最后**而不是开头——`app/spd/models.py` 要用本包的
# `Money` 与 `utcnow`，此刻它们已经定义完毕。
# ============================================================================
from ..spd.models import *  # noqa: E402,F401,F403

from typing import TYPE_CHECKING  # noqa: E402

if not TYPE_CHECKING:  # 只在运行期生效：给 mypy 看见的话，任何写错的 `from app.models import X` 都会被当成 Any 放过

    def __getattr__(name: str):
        """子系统模型的回落：上面那句星号导入若执行时 `app.spd.models` 还只初始化到一半，就补上它漏掉的名字。

        先 import `app.spd.models`、后 import 本包的进程里，本包是被子系统模型自己那句 `from ..models import Money, utcnow`
        拉起来的——此刻子系统模型刚执行到那一行，星号导入拿到的是半个模块，本包上一个 `Spd*` 都没有。应用自己总是先
        import 本包，碰不上；测试碰得上：单跑几个文件时，先被收集的那个若先 import 了 `app.spd.models`，之后所有
        `app.models.SpdProgram` / `from app.models import SpdEnrollment` 一律 AttributeError / ImportError，报在与肇事文件
        毫不相干的用例上。属性缺失时才走到这里，那时子系统模型早已初始化完。
        """
        from ..spd import models as spd_models

        try:
            return getattr(spd_models, name)
        except AttributeError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
