"""桌面端慢病在管名单固定只取前 500 份、不提示截断（P2-1740，第五十一批扫描 AO4-4）。

慢病页 `core.js::renderChronic` 取在管名单是 `api("/api/chronic")`，不带 limit——后端 `list_chronic` 缺省一页 500 份、按分级
从高到低再按档案号排，总数只在 X-Total-Count 里，页面不读。扫描实测（修前代码，`r3_history_level.py`）：直接落库 502 份后
`GET /api/chronic` 回 500 条、X-Total-Count 502；表格不提示截断，排在后面的 1 级档案在页面上没有行，「风险评分」「随访记录」
两个按钮够不着；标题里的「N 人随访超期」却按全部算（下面那位超期的 1 级档案就不在这一页上）。

修法（照签约页 P2-1547）：经 `api(…, { withTotal: true })` 读 X-Total-Count，列不全时标题写「共 N 份，仅列前 M 份」，与超期
人数同在一个括号里；没截断时标题一字不变。不改取数范围、不加翻页（名单给谁看、全量怎么翻随 P1-49 定），后端不动。
页面原样拿到 node 里跑（`chronic_followup_pages`），响应头照真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from chronic_followup_pages import function_source, run
from conftest import business_today

from app.database import SessionLocal
from app.models import ChronicPatient, Patient

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 修前标题的写法：没截断时一字不变
OVERDUE_TITLE = '<h3>在管名单（<span style="color:#c62828">1 人随访超期</span>）</h3>'


def _archives(org: int, start: int, levels: list[int], next_due: str) -> list[int]:
    """直接落库几份高血压档案（每份一位新患者），返回档案号。"""
    with SessionLocal() as db:
        rows = []
        for n, level in enumerate(levels, start=start):
            patient = Patient(ehc_no=f"P21740{n:05d}", name=f"P21740 患者{n}", id_card=f"33010619600101{n:04d}")
            db.add(patient)
            db.flush()
            rows.append(ChronicPatient(patient_id=patient.id, disease="hypertension", level=level,
                                       managed_by_org_id=org, next_due=next_due))
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def _render(client, admin) -> str:
    return run(client, admin, "admin", "await renderChronic(); return pageHtml();")


def _title(html: str) -> str:
    (title,) = re.findall(r"<h3>在管名单.*?</h3>", html)
    return title


def test_取在管名单读总数():
    body = function_source((STATIC / "core.js").read_text(encoding="utf-8"), "async function renderChronic(")
    assert 'api("/api/chronic", { withTotal: true })' in body   # 修前 api("/api/chronic")，读不到 X-Total-Count


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_过了500份标题写明截断_没截断时标题照旧(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21740 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    later = (business_today() + timedelta(days=30)).isoformat()
    (high,) = _archives(org, 0, [3], later)
    (overdue,) = _archives(org, 1, [1], (business_today() - timedelta(days=1)).isoformat())   # 1 级、已超期
    html = _render(client, admin)
    assert _title(html) == OVERDUE_TITLE   # 两份，没截断：标题与修前一字不差
    assert html.count('data-fuhist="') == 2

    _archives(org, 2, [2] * 500, later)   # 共 502 份：按分级排，3 级一份、2 级 499 份占满这一页
    resp = client.get("/api/chronic", headers=admin)
    assert (len(resp.json()), resp.headers["X-Total-Count"]) == (500, "502")   # 后端不动
    html = _render(client, admin)
    assert _title(html) == ('<h3>在管名单（共 502 份，仅列前 500 份；'
                            '<span style="color:#c62828">1 人随访超期</span>）</h3>'), _title(html)   # 修前同上面两份时
    assert html.count('data-fuhist="') == 500
    assert f'data-fuhist="{high}"' in html
    assert f'data-fuhist="{overdue}"' not in html   # 超期算进了标题，这一页上却没有它的行——所以要写明列不全
