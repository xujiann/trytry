"""人员下沉调度页「维护职称等级」的下拉选项与后端同一张表（P2-38）。

这一处原先要手打英文代码（junior/intermediate/…），打错被后端 422 拒回；换成下拉后，
选项是前端的一张映射（与同文件的 ASSIGN_TYPES 同一种写法）。前端多一个后端不认的值，
选了就是 422；后端加了一档前端没有，那一档就永远选不到——所以把两边钉在一起。
"""
import re
from pathlib import Path

from app.routers.staffing import TITLE_LEVELS

MGMT_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js"


def _frontend_levels() -> dict[str, str]:
    m = re.search(r"const TITLE_LEVELS = \{([^}]*)\}", MGMT_JS.read_text(encoding="utf-8"))
    assert m, "pages-mgmt.js 里找不到 TITLE_LEVELS 映射，判据失灵"
    return dict(re.findall(r'(\w+): "([^"]*)"', m.group(1)))


def test_前端职称等级选项与后端同一张表():
    assert _frontend_levels() == TITLE_LEVELS


def test_判据自证_解析得到五档():
    levels = _frontend_levels()
    assert len(levels) == 5 and levels["deputy_senior"] == "副高", levels
