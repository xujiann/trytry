"""二维码 SVG 生成（平台公共件）。

原实现长在 `app/spd/routers/config/_base.py`——那是子系统内部，平台侧
（打印件验真，ADR-0015）按依赖方向**不能**去 import 它。把实现上移到平台、
spd 经 `platform.py` 再导出后委托，方向就顺了：spd→平台是白名单内的合法方向，
平台→spd 才是被 `tests/test_spd_boundary.py` 禁止的那条。

`qrcode` 是纯 Python 实现（无 Pillow 也能出 SVG），符合"不引重依赖"的约束；
SVG 而不是 PNG：打印件与培训海报都要放大，位图会糊。
"""
import io


def qr_svg(content: str) -> str:
    """把一段文本编成二维码 SVG 字符串。"""
    import qrcode  # 延迟导入：不用二维码的进程不必付导入成本
    import qrcode.image.svg

    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage, box_size=16)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
