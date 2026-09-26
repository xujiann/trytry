"""前端取「今天 / 本月」不得用 UTC（P2-228）。

`new Date().toISOString()` 是 UTC 时间：东八区早上 8 点前 `.slice(0, 10)` 取到的是昨天，每月 1 日早上 8 点前
`.slice(0, 7)` 取到的是上个月。2026-09-26 读到 9 处：慢专病复诊「完成」时写进库的实际复诊日期（早上 7 点半办结的
复诊记成前一天）、日终对账的默认对账日、会计 / 成本 / 运营分析 / 医疗质量与用药四页的默认月份、考核计分的默认周期、
审计与任务导出文件名里的日期。后端 `clock.today()` 早就写明「刻意不用 UTC」（P2-171 是后端的同一形状）。

修法：`shared.js` 给一个 `localToday()`（本地日历的 `YYYY-MM-DD`，本月取 `.slice(0, 7)`），九处改取它。本用例盯两件事：
一、三套前端（含居民端、医生端）不再出现拿 `toISOString()` 截日期 / 月份的写法（零基线，只减不增）；
二、`localToday` 只在 shared.js 定义一份（shared.js 排在每个入口的第一个 script，见 test_frontend_shared_utils）。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SOURCES = sorted([*STATIC.glob("*.js"), *STATIC.glob("*.html"), *STATIC.glob("m/*.js"), *STATIC.glob("m/*.html")])

#: `toISOString().slice(0, 10)` / `.substring(0, 7)` / `.split("T")[0]` 一类：拿 UTC 的时间串截出日期或月份
UTC_DATE = re.compile(
    r"toISOString\(\)\s*\.\s*(?:(?:slice|substring|substr)\(\s*0\s*,\s*(?:7|10)\s*\)|split\(\s*['\"]T['\"]\s*\))")


def test_前端不拿UTC时间截今天或本月():
    hits = [f"{path.relative_to(STATIC)}:{no}: {line.strip()}"
            for path in SOURCES
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if UTC_DATE.search(line)]
    assert hits == [], "取「今天 / 本月」用 shared.js 的 localToday()，别用 UTC：\n" + "\n".join(hits)


def test_闸门认得出修前的写法():
    for line in ('body.actual_date = new Date().toISOString().slice(0, 10);',
                 'const thisMonth = new Date().toISOString().slice(0, 7);',
                 "const d = new Date().toISOString().split('T')[0];"):
        assert UTC_DATE.search(line), line
    assert not UTC_DATE.search("const at = new Date().toISOString();")   # 完整的 UTC 时间戳（带时区）不在此列


def test_localToday只在shared_js定义一份():
    defs = [path.name for path in SOURCES
            if re.search(r"^\s*(?:function localToday\(|(?:const|let|var) localToday\s*=)",
                         path.read_text(encoding="utf-8"), re.M)]
    assert defs == ["shared.js"], defs
