"""医生移动端三处小毛病（P2-374）。

- 档案速查：每段静默只留 20 条，也不看后端的 `has_more` / `section_limit`（后端每段最多给 50 条并标出截断）；病种显示编码。
  电脑端同一接口早就提示「以下分段超过 N 条已截断」。
- 未读消息角标：取的是这一页（`limit=20`）的条数，积压过 20 条也只显示 20；`/api/notifications/unread-count` 早就有。
- 慢病随访录入：每条分级规则画一个输入框——种子糖尿病的空腹血糖挂着「偏高」与「低血糖」两条（P2-119），画出两个框，
  两个框填同一个键，提交时后一个静默盖掉前一个。

修法：截断说出来、病种取目录名称；角标取未读总数；同一指标只画一个框。
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


def _slice(start_marker, end_marker="\n}\n"):
    start = SRC.index(start_marker)
    return SRC[start:SRC.index(end_marker, start)]


def test_未读角标取未读总数():
    body = _slice("async function loadTodos()")
    assert "/api/notifications/unread-count" in body, body
    assert '<span class="badge warn">${unread.unread}</span>' in body, body   # 修前 ${notices.length}


def test_同一指标只画一个输入框():
    body = _slice("function renderMetricInputs()")
    assert "seen.has(m.key)" in body, body   # 修前每条规则一个框


def test_种子里确有同一指标挂两条规则():
    """去重的由来：种子糖尿病的空腹血糖两条规则（偏高 / 低血糖）键都是 glucose。"""
    from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES

    diabetes = next(t for t in SEED_CHRONIC_DISEASE_TYPES if t["code"] == "diabetes")
    keys = [m["key"] for m in diabetes["level_rules"]["metrics"]]
    assert keys.count("glucose") == 2, keys


def test_档案速查说出截断_病种显示名称():
    body = _slice('$("#pt-form").addEventListener("submit"', "\n});\n")
    assert "data.has_more" in body and "data.section_limit" in body, body   # 修前静默截断
    assert "/api/chronic/disease-types" in body and "byCode[c.disease]" in body, body   # 修前显示编码
