"""配置域的共享底座：路由对象 + 跨子模块共用的小工具。

拆包时（ADR-0008）单独立出来的原因很实在：`_bump_version` 被「专病档案」与
「标准化指导路径」两节用、`_qr_svg` 被「评估量表」与「村医档案」两节用。
它们本来就是跨节的公共件，只是原来混在一个大文件里看不出来——
放这里让「谁是公共件」变成明摆着的事，而不是靠 grep。
"""


from fastapi import APIRouter, Depends, HTTPException

from ....deps import get_current_user
from ...rules import RuleError, validate_conditions


router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·配置"],
    dependencies=[Depends(get_current_user)],
)

CONFIG_ROLES = ("director", "doctor")


def _conditions(raw: list[dict] | None) -> list[dict]:
    try:
        return validate_conditions(raw or [])
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _bump_version(version: str) -> str:
    """v1 → v2。非 v 开头的自定义版本号原样保留并追加 -r2，不猜用户的编号规则。"""
    if version.startswith("v") and version[1:].isdigit():
        return f"v{int(version[1:]) + 1}"
    return f"{version}-r2"


def _qr_svg(content: str) -> str:
    """把一段文本编成二维码 SVG。

    `qrcode` 是纯 Python 实现（无 Pillow 也能出 SVG），符合"不引重依赖"的约束；
    SVG 而不是 PNG：打印培训海报要放大到 A4，位图会糊。
    """
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage, box_size=16)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
