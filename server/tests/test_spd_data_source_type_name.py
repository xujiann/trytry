"""数据源接入表的「类型」列原样印 publichealth / checkup / device（P2-1025，第二十九批「前后端取值表」扫描 E1-7，与 P2-684 同形）。

同一页新建数据源的下拉用的是 `SPD_DS_TYPES`（公卫随访 / 体检 / 设备回传……），接入表的「类型」列却直接插编码。修后列也按
`SPD_DS_TYPES` 译，表外的值原样回显。
"""
import re
from pathlib import Path

from app.spd.routers.config.devices import DataSourceIn

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_类型表与后端放行的类型同一套():
    table = re.search(r"^const SPD_DS_TYPES = \{(.*?)\};", PAGE, re.M | re.S).group(1)
    codes = set(re.findall(r'(\w+): "', table))
    pattern = DataSourceIn.model_json_schema()["properties"]["source_type"]["pattern"]
    assert codes == set(pattern.strip("^()$").split("|"))


def test_接入表的类型列按中文名印():
    assert "<td>${esc(SPD_DS_TYPES[src.source_type] || src.source_type)}</td>" in PAGE
    assert "<td>${esc(src.source_type)}</td>" not in PAGE   # 修前原样印编码
