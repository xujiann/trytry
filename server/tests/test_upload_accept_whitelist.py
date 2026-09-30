"""页面上传框的 `accept` 与附件白名单同一串（P2-1083，第三十一批「页面输入约束 vs 后端校验」扫描 C3-5）。

居民任务凭证（m.js）、医生端佐证（doctor.js）、管理端慢专病任务佐证（pages-spd.js）三处写的是 `image/*,.pdf`：手机
「高效格式」拍的 HEIC、扫描仪出的 TIFF / BMP 都选得上，传上去才 415「附件类型不在白名单」——居民不知道怎么转格式，
要凭证才能办结的任务就卡在这里。管理端另外四处附件入口写的正是白名单。静态扫全部上传框，收图片 / PDF 的必须与后端同一串。
"""
import re
from pathlib import Path

from app.routers.attachments import ALLOWED_CONTENT_TYPES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
ACCEPT = re.compile(r'accept(?:="|\s*=\s*")([^"]*)"')


def test_收图片或PDF的上传框_accept与附件白名单同一串():
    seen, wrong = 0, []
    for path in sorted(STATIC.rglob("*")):
        if path.suffix not in (".js", ".html"):
            continue
        for value in ACCEPT.findall(path.read_text(encoding="utf-8")):
            if "image" not in value and "pdf" not in value:
                continue
            seen += 1
            if set(value.split(",")) != ALLOWED_CONTENT_TYPES:
                wrong.append(f"{path.relative_to(STATIC)}: {value}")
    assert seen >= 7, seen   # 管理端四处 + 三端慢专病凭证三处；扫不到说明写法变了、闸门失效
    assert wrong == [], wrong   # 修前三处 image/*,.pdf
