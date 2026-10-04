"""机构层级「市级」在页面上显示成 city（P2-428）。

前端的机构层级文案表 `LEVELS`（core.js）只有县 / 乡 / 村三级，后端 `organizations.ORG_LEVEL_NAMES` 与模型列注释是四级
（多一个 city=市级，市级合作医院；乡镇卫生院的上级可以是县级或市级）。于是机构列表、考核排名、机构树体检的
「应挂在」一栏里，市级都显示成 `city`；建机构的层级下拉也选不到市级。修后前端表与后端同一份，这里钉住。
"""
import re
from pathlib import Path

from app.routers.organizations import ORG_LEVEL_NAMES

CORE = Path(__file__).resolve().parents[1] / "app" / "static" / "core.js"


def _frontend_levels() -> dict[str, str]:
    match = re.search(r"const LEVELS = \{([^}]*)\};", CORE.read_text(encoding="utf-8"))
    assert match, "core.js 里找不到 LEVELS"
    return dict(re.findall(r'(\w+): "([^"]*)"', match.group(1)))


def test_前端机构层级表与后端同一份():
    assert _frontend_levels() == ORG_LEVEL_NAMES   # 修前缺 city


def test_县级排在建机构层级下拉的第一个选项():
    """补 city 时排在末尾，原意是建机构表单的缺省层级仍是县级；P1-247 起层级下拉不给缺省、必选
    （test_org_create_parent_level 盯着），县级仍排在实际选项的第一个。"""
    assert next(iter(_frontend_levels())) == "county"
