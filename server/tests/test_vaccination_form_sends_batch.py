"""接种登记表单送批次、接种部位与接种人（P1-154）。

接种登记接口给了批次就「三查」（过期 / 封存 / 库存）并原子扣一支、把批号记到这一针上；模型注释写「新接种一律建议带
批号——出了问题按批号召回时，没批号的记录查不出来」。而界面上唯一的接种登记表单从不送 `batch_id`（也不送部位、
接种人）：封存的批次照样打、库存不扣，按批号反查受种者这一针永远查不到。端到端档驱动下拉选批次 → 带出疫苗与机构 →
登记即扣一支（`tests/e2e/test_flows.py`）。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _render_vaccination() -> str:
    with open(os.path.join(STATIC, "pages-clinical.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderVaccination()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_表单有批次下拉与部位_接种人():
    body = _render_vaccination()
    form = body[body.index('id="vac-form"'):]
    form = form[:form.index("</form>")]
    for field in ('<select name="batch_id" id="vac-batch">', 'name="site"', 'name="vaccinator"'):
        assert field in form, field                                              # 修前三格都没有


def test_批次按数字送出_下拉只列可用批次():
    body = _render_vaccination()
    assert 'formJson(e.target, ["patient_id", "dose_no", "org_id", "batch_id"])' in body
    assert 'api("/api/vaccine-supply/batches?usable_only=true")' in body
