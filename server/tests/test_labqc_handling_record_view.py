"""室内质控失控处理登记之后，L-J 表上只剩一个「已处理」：原因、纠正措施、处理人与操作者都看不见（P2-492）。

`GET /api/labqc/lots/{lot_id}/measurements` 带着这几项（`MeasurementOut`），前端一个调用都没有（读动词棘轮 P2-475
登记在册）；L-J 数据（`LjPoint`）只带「是否已处理」。失控处理的意义全在纠正措施的记录，原先录进去就没处看。

修法：打开批号时并取测定值清单，按 id 对上：表格补「操作者」一列，已处理的失控点在「处理」一格写明原因、纠正
措施与处理人。两份取的是同一个最近 500 点的窗口（`_latest_measurements`）。
"""
from pathlib import Path

from app.routers.labqc import LjPoint, MeasurementOut

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_labqc() -> str:
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderLabQc()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_打开批号时并取测定值清单_处理记录看得见():
    body = _render_labqc()
    draw = body[body.index("const drawLot = async (lotId) => {"):]
    assert "api(`/api/labqc/lots/${lotId}/measurements`)" in draw[:600]   # 修前没有一个 GET 调用
    for field in ("m.handle_reason", "m.corrective_action", "m.handled_by", "m.operator"):
        assert field in draw, field


def test_L_J点不带的处理字段_清单都带():
    missing_in_lj = {"handle_reason", "corrective_action", "handled_by", "operator"} - set(LjPoint.model_fields)
    assert missing_in_lj == {"handle_reason", "corrective_action", "handled_by", "operator"}
    assert missing_in_lj <= set(MeasurementOut.model_fields)
