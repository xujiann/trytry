"""体温单的每一项测量值，桌面与查房移动端都录得进、也看得见（P2-473）。

体温单模型（`VitalSignRecord`）与入参（`VitalIn`）一直有出入量（intake_ml / output_ml）与体重（weight_kg），
桌面「住院文书」的体温单表单与表格、医生移动端的查房体征都只管体温、脉搏、呼吸、血压——这三项录不进、也看不见，
全仓没有一个页面或打印模板读它们。

这里按入参模型派生：`clinical_docs.VITAL_VALUES`（除测量时刻与记录人外的全部字段）逐项核对四处——
桌面表单、桌面表单按数送的字段、桌面表格、移动端录入与展示。以后模型加一项测量值，页面没跟上即红。
"""
import re
from pathlib import Path

from app.routers.clinical_docs import VITAL_VALUES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _clinical_docs_page() -> str:
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderClinicalDocs()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_桌面体温单表单每一项都有输入框():
    page = _clinical_docs_page()
    form = page[page.index('<form class="inline" id="vital-form">'):]
    form = form[:form.index("</form>")]
    missing = set(VITAL_VALUES) - set(re.findall(r'name="(\w+)"', form))
    assert not missing, f"体温单表单缺这几项的输入框（修前缺出入量与体重）：{sorted(missing)}"


def test_桌面体温单每一项都按数送():
    page = _clinical_docs_page()
    call = re.search(r"postAction\(`/api/inpatient/admissions/\$\{current\}/vitals`,.*?formJson\(e\.target, \[(.*?)\]\)",
                     page, re.S)
    assert call, "体温单提交的写法换了，本用例要跟着改"
    missing = set(VITAL_VALUES) - set(re.findall(r'"(\w+)"', call.group(1)))
    assert not missing, f"这几项没按数送（字符串会被 422）：{sorted(missing)}"


def test_桌面体温单表格每一项都看得见():
    page = _clinical_docs_page()
    table = page[page.index('table(["测量时刻"'):]
    table = table[:table.index("</tr>`)")]
    missing = {name for name in VITAL_VALUES if f"v.{name}" not in table and name not in ("sbp", "dbp")}
    assert not missing, f"体温单表格看不见这几项（修前看不见出入量与体重）：{sorted(missing)}"
    assert "v.sbp" in table and "v.dbp" in table


def test_查房移动端录得进也看得见():
    script = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    html = (STATIC / "m" / "doctor.html").read_text(encoding="utf-8")
    loop = script[script.index('$("#round-vital").addEventListener("submit"'):]
    loop = loop[:loop.index("try {")]
    pairs = dict(re.findall(r'\["(\w+)", "#([\w-]+)"\]', loop))
    missing = set(VITAL_VALUES) - set(pairs)
    assert not missing, f"查房体征录入缺这几项（修前缺出入量与体重）：{sorted(missing)}"
    for field, element in pairs.items():
        assert f'id="{element}"' in html, f"{field} 的输入框 #{element} 不在页面上"
    shown = script[script.index('$("#round-vitals").innerHTML'):]
    shown = shown[:shown.index("尚无体征记录")]
    missing = {name for name in VITAL_VALUES if f"v.{name}" not in shown}
    assert not missing, f"查房体征卡片看不见这几项：{sorted(missing)}"
