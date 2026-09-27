"""422 的「超长 / 选多了」报人话（P2-606，第十二批「前端提示 vs 后端校验」扫描 Z3-7）。

模态框里的多行文本没有字数上限，写超了后端 422，`errorText` 原先只认「只填空格」「留空」两种，超长照样是英文原话
「String should have at most 2048 characters」；批量选人超过上限是「List should have at most 500 items after
validation, not 501」。按错误类型与约束换成「最多 N 个字 / 项，超出了」。

（模态框提交即关框、422 之后填的内容要重填，是另一件事：要改 spdModal 的调用约定，登记为 P2-607。）
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SHARED = (Path(__file__).resolve().parents[1] / "app" / "static" / "shared.js").read_text(encoding="utf-8")


def _error_text_source() -> str:
    start = SHARED.index("function errorText(detail, fallback) {")
    return SHARED[start:SHARED.index("\n}\n", start) + 2]


@pytest.fixture(scope="module")
def details(client, admin):
    """真实的 422 形状：超长的文本、选多了的名单（请求体校验在处理函数之前，患者编号是什么都一样）。"""
    too_long = client.post("/api/spd/interventions", headers=admin, json={"patient_ids": [1], "content": "字" * 2049})
    too_many = client.post("/api/spd/interventions", headers=admin, json={"patient_ids": [1] * 501, "content": "低盐"})
    assert too_long.status_code == too_many.status_code == 422
    return too_long.json()["detail"], too_many.json()["detail"]


def test_后端的422确实带类型与上限(details):
    too_long, too_many = details
    assert (too_long[0]["type"], too_long[0]["ctx"]["max_length"]) == ("string_too_long", 2048)
    assert (too_many[0]["type"], too_many[0]["ctx"]["max_length"]) == ("too_long", 500)


def test_errorText认出超长与选多了():
    body = _error_text_source()
    assert 'e.type === "string_too_long" && ctx.max_length != null' in body
    assert 'e.type === "too_long" && ctx.max_length != null' in body


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍errorText_报的是人话(details):
    too_long, too_many = details
    script = _error_text_source() + "\nconsole.log(errorText(JSON.parse(process.argv[1]), '兜底'));"
    out = [subprocess.run(["node", "-e", script, json.dumps(d)], capture_output=True, text=True, check=True).stdout.strip()
           for d in (too_long, too_many)]
    assert out == ["content：最多 2048 个字，超出了", "patient_ids：最多 500 项，超出了"]   # 修前是英文原话
