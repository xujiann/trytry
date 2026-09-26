"""用药画像查询不接错误：查不到（404）或无权查看（403）时页面一声不吭，上一位患者的画像与多重用药提示照旧挂着（P2-358）。

`api()` 遇非 2xx 抛错（core.js），`#prof-form` 的提交处理直接 `await api(...)`、没有 try/catch——换一个输错的患者号查，
页面上还是上一位的结果，而「多重用药风险」那一行不写是谁，看的人会以为是这一位。
修法：查询前先清空结果区，查不到把原因写出来；提示行写明患者号。
"""
from pathlib import Path

CLINICAL = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _handler() -> str:
    start = CLINICAL.index('$("#prof-form").onsubmit')
    return CLINICAL[start:CLINICAL.index("\n  };\n", start)]


def test_查询前清空结果_查不到写出原因():
    body = _handler()
    clear = body.index('$("#prof-result").innerHTML = "";')
    call = body.index("await api(`/api/medication/profile/")
    assert clear < call                         # 先清掉上一位的结果
    assert "catch (err)" in body and "esc(err.message)" in body   # 修前没有 try/catch


def test_多重用药提示写明是哪一位():
    line = next(x for x in _handler().splitlines() if "多重用药风险：同时在用" in x)
    assert "profile.patient_id" in line
