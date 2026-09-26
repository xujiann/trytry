"""居民端复诊状态的键写错了：超期的显示成灰色英文 overdue、结案移除的显示成 removed（P2-364）。

`spd_revisits.status` 的取值是 planned / done / overdue / removed（模型列注释；超期扫描写 overdue，结案收尾写 removed）。
居民端 `SPD_REVISIT_STATUS` 却写成 planned / done / missed / cancelled——后两个后端从不写，前两个真实取值查不到名字，
标签原样显示英文。管理端同一张表的映射一直是对的。这里钉「前端映射的键 == 模型列注释列出的取值」。
"""
import inspect
import re
from pathlib import Path

from app.spd import models

M_JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8")


def _model_statuses() -> set[str]:
    src = inspect.getsource(models.SpdRevisit)
    comment = next(line for line in src.splitlines() if "planned=" in line)
    return set(re.findall(r"(\w+)=", comment))


def _js_keys() -> set[str]:
    block = re.search(r"const SPD_REVISIT_STATUS = \{(.*?)\};", M_JS, re.S).group(1)
    return set(re.findall(r"(\w+): \[", block))


def test_居民端复诊状态的键就是后端的取值():
    assert _model_statuses() == {"planned", "done", "overdue", "removed"}
    assert _js_keys() == _model_statuses()   # 修前 {planned, done, missed, cancelled}
