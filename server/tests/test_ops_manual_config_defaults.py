"""运维手册的配置表写 `MEDPLAT_PORTAL_LEGACY_VERIFY` 缺省为 true，代码缺省是 false（P2-994，第二十八批「缺省值」扫描 F4-8）。

P1-3 把这个兼容开关翻成默认关（`config.py`），手册 §2 的配置表没跟：仍在过渡期、居民账号还没铺开的县照手册以为旧核验默认
开着、不设这个变量，居民端旧入口全是 410。手册配置表其余各项与 `config.py` 一致（逐项比对）。

修法：手册与接口对接规范改成「默认关、过渡期显式置 true」；这条用例逐项比对手册配置表里每个 `Settings` 字段的缺省值与代码，
以后谁改了缺省没改手册（或反过来）就红。不在 `Settings` 里的变量（直接读环境变量的几项）不比。
"""
import re
from pathlib import Path

from app.config import Settings

MANUAL = (Path(__file__).resolve().parents[2] / "docs" / "运维手册.md").read_text(encoding="utf-8")
ROW = re.compile(r"^\| `(MEDPLAT_[A-Z_]+)`(?: / `(MEDPLAT_[A-Z_]+)`)? \| ([^|]+) \|", re.M)


def _doc_default(cell: str) -> str | None:
    cell = cell.strip()
    if cell == "空":
        return ""
    quoted = re.match(r"`([^`]*)`", cell)
    return quoted.group(1) if quoted else None


def _code_default(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def test_手册配置表的缺省值与代码一致():
    fields = Settings.model_fields
    compared, mismatched = 0, []
    for match in ROW.finditer(MANUAL):
        doc = _doc_default(match.group(3))
        for var in filter(None, (match.group(1), match.group(2))):
            field = fields.get(var[len("MEDPLAT_"):].lower())
            if field is None or doc is None:
                continue
            compared += 1
            if _code_default(field.default) != doc:
                mismatched.append(f"{var}：手册 {doc!r}，代码 {_code_default(field.default)!r}")
    assert compared >= 40, f"只比对到 {compared} 项，手册的配置表格式变了？"
    assert not mismatched, "\n".join(mismatched)   # 修前 MEDPLAT_PORTAL_LEGACY_VERIFY：手册 'true'，代码 'false'
