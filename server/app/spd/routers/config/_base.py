"""配置域的共享底座：路由对象 + 跨子模块共用的小工具。

拆包时（ADR-0008）单独立出来的原因很实在：`_bump_version` 被「专病档案」与
「标准化指导路径」两节用、`_qr_svg` 被「评估量表」与「村医档案」两节用。
它们本来就是跨节的公共件，只是原来混在一个大文件里看不出来——
放这里让「谁是公共件」变成明摆着的事，而不是靠 grep。
"""


from fastapi import APIRouter, Depends, HTTPException, Response

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


class SvgResponse(Response):
    """带 `media_type` 的 SVG 响应。

    既当 `response_class=`（把媒体类型写进 OpenAPI，也让契约棘轮认得出这是
    "我返回 image/svg+xml 字节流"的声明），也是二维码端点实际返回的类——
    声明与实际返回是同一个类，不会各说各话。写法与 `reports.CsvResponse` 一致。
    """

    media_type = "image/svg+xml"


def _qr_svg(content: str) -> str:
    """把一段文本编成二维码 SVG。

    实现已上移平台侧 `app/qrsvg.py`（ADR-0015 打印件验真也要用，而平台侧
    按单向依赖不能来 spd 里拿）；这里经 `platform.py` 委托，名字与签名保持
    原样——spd 内部的调用点一行不用改。
    """
    from ...platform import qr_svg

    return qr_svg(content)
