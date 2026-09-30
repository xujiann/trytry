"""居民端卡片内表单一提交就先移除：请求失败只弹一句，逐题作答和完成情况全没了（P2-1014，第二十九批「页面状态残留」扫描 E2-7）。

`inlineInput` / `inlineQuestions` 原先 `form.remove()` 之后才把值交给调用方，调用方再发请求——任务填报、线上自助随访、
干预反馈失败时（如这条随访其间已被医护执行，409）只 `alert` 一句。医生端 `cardForm` 写明「表单留到提交成功……后端报错时
填过的字还在，改了再交」，管理端 P2-607 也把弹窗改成了框内提交、失败留框。

修法：两个帮手收一个提交回调，交给 `inlineSubmit`：成功才收起，失败把原因写进表单里的消息行；三个调用处把请求放进回调。
端到端用例在 `tests/e2e/test_flows.py`（打开表单后医护执行了这一条，再提交：表单留着、原因写在里面、填过的还在）。
"""
from pathlib import Path

from jssrc import strip_comments

SRC = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8"))


def _fn(marker):
    start = SRC.index(marker)
    return SRC[start:SRC.index("\n}\n", start)]


def test_提交成功才收起表单_失败把原因写进表单():
    body = _fn("async function inlineSubmit(")
    success = body.rindex("form.remove();")   # 第一处是不带回调的老写法：交值即收起
    assert body.index("await submit(value);") < success
    catch = body[body.index("catch (err)"):success]
    assert "msg.textContent = err.message;" in catch and "return;" in catch


def test_两个帮手交值都走_inlineSubmit():
    for marker in ("function inlineInput(", "function inlineQuestions("):
        body = _fn(marker)
        assert "inlineSubmit(form," in body, marker
        assert "data-inline-msg" in body, marker
        onsubmit = body[body.index("form.onsubmit"):body.index('form.querySelector("[data-cancel]")')]
        assert "form.remove()" not in onsubmit, f"{marker} 交值时又先移除了表单"


def test_三个调用处把请求放进提交回调():
    for api in ("/api/portal/spd/tasks/", "/api/portal/spd/followups/", "/api/portal/spd/interventions/"):
        at = SRC.index(api)
        before = SRC[:at]
        # 请求写在 submit 回调（或自助随访的 send）里，而不是等表单交完值之后
        assert before.rfind("submit: (") > before.rfind("await inline") or before.rfind("const send = (") > before.rfind(
            "await inline"), api
