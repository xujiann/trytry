"""422 的数值上下界、整数 / 数字解析、缺字段、格式、取值范围报人话（P2-1087，第三十一批「页面输入约束 vs 后端校验」扫描 C3-2）。

`errorText` 原先只认「只填空格 / 留空 / 超长 / 选多了」四类（P1-109、P1-110、P2-606），其余照样是 pydantic 的英文原话，
而产检孕周、儿童体重、续方处方号、绩效权重、定时任务间隔、术中出血量六处页面注释都写着「交给后端报人话」。实测（修前）：
孕周填「38+2」→ `gest_week：Input should be a valid integer, unable to parse string as an integer`；体温敲成 365 →
`temperature：Input should be less than or equal to 45`；报告推送时间填「8:00」→ `push_time：String should match pattern
'^([01][0-9]|2[0-3]):[0-5][0-9]$'`；服务包扣减 1.5 次 → `Input should be a valid integer, got a number with a fractional part`。
按错误类型换成人话；字段名照旧（是哪一格仍要看得出），认不出的类型照旧取原话。
"""
import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

SHARED = (Path(__file__).resolve().parents[1] / "app" / "static" / "shared.js").read_text(encoding="utf-8")


def _error_text_source() -> str:
    start = SHARED.index("function errorText(detail, fallback) {")
    return SHARED[start:SHARED.index("\n}\n", start) + 2]


class _Probe(BaseModel):
    """与各处请求模型同一种约束写法：pydantic 生成的错误即 FastAPI 422 的 `detail` 形状。"""

    gest_week: int = Field(ge=4, le=45)
    temperature: float = Field(ge=30, le=45)
    push_time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    qty: int
    status: Literal["pending", "done"]
    note: str


def _details(payload: dict) -> list:
    with pytest.raises(ValidationError) as exc:
        _Probe.model_validate(payload)
    return json.loads(exc.value.json())


def _render(detail) -> str:
    script = _error_text_source() + "\nconsole.log(errorText(JSON.parse(process.argv[1]), '兜底'));"
    return subprocess.run(["node", "-e", script, json.dumps(detail)], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
@pytest.mark.parametrize(("payload", "expected"), [
    ({"gest_week": "38+2"}, "gest_week：要填整数"),
    ({"gest_week": 3}, "gest_week：不能小于 4"),
    ({"temperature": 365}, "temperature：不能大于 45"),
    ({"temperature": "3.5kg"}, "temperature：要填数字"),
    ({"push_time": "8:00"}, "push_time：格式不对"),
    ({"qty": 1.5}, "qty：要填整数，不能带小数"),
    ({"status": "closed"}, "status：取值不在可选范围内"),
    ({}, "note：必填"),
])
def test_常见的422换成人话(payload, expected):
    good = {"gest_week": 38, "temperature": 36.5, "push_time": "08:00", "qty": 1, "status": "pending", "note": "x"}
    base = {**good, **payload}
    if not payload:
        base.pop("note")
    detail = _details(base)
    text = _render(detail)
    assert text == expected, (detail, text)                  # 修前是英文原话
    assert "Input should" not in text and "String should" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_认不出的类型照旧取原话_多条照旧用分号连起来():
    detail = [{"type": "uuid_parsing", "loc": ["body", "ref"], "msg": "Input should be a valid UUID", "ctx": {}},
              {"type": "int_parsing", "loc": ["query", "limit"], "msg": "…", "ctx": {}}]
    assert _render(detail) == "ref：Input should be a valid UUID；limit：要填整数"


def test_定时任务间隔在页面上按分钟判下界():
    """送的是秒（分钟 × 60），后端的下界报成「interval_seconds：不能小于 60」，填分钟的人看不懂——页面先按分钟判。"""
    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")
    handler = source[source.index('spdModal("改执行间隔"'):]
    handler = handler[:handler.index("interval_seconds: Math.round(form.minutes * 60)")]
    assert 'if (!(form.minutes >= 1)) throw new Error("执行间隔至少 1 分钟");' in handler
