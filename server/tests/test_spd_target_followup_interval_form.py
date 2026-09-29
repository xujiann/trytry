"""管理目标的新增表单与编辑弹窗补「随访周期（天）」、清单显示这一列（P2-856，第二十三批「页面表单提交的字段与取值 vs 后端
请求模型」扫描 Y1-5）。

随访任务办结后，下次随访日 = 今天 + 本阶段管理目标的 `followup_interval_days`（`_followup_interval` 的 docstring：随访周期按
阶段定，治疗期一月一次、稳定期一季一次）；`TargetIn` 缺省 90、`TargetPatch` 也收。页面的新增表单与编辑弹窗都没有这一项、
清单也不显示：界面配的目标一律 90 天，治疗期「一月一次」配不出，种子的 30 天也改不了。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_新增表单送随访周期():
    start = PAGE.index('<form class="inline" id="spd-target-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<input name="followup_interval_days" type="number"' in form   # 修前没有
    assert 'formJson(e.target, ["target_low", "target_high", "followup_interval_days"])' in PAGE


def test_清单显示随访周期_编辑弹窗能改():
    assert '"定性", "随访周期", "操作"]' in PAGE and "${t.followup_interval_days} 天" in PAGE
    start = PAGE.index('spdModal("编辑管理目标（留空的项不改）"')
    body = PAGE[start:PAGE.index("/api/spd/targets/${targetEdit.dataset.targetEdit}", start)]
    assert '{ name: "followup_interval_days", label: "随访周期（天，留空不改）", type: "text" }' in body
    assert "body.followup_interval_days = days;" in body


def test_按页面送的周期建目标_接口照收(client, admin):
    program = client.post("/api/spd/programs", headers=admin, json={
        "code": "p2856_prog", "name": "P2856 病种", "category": "chronic"})
    assert program.status_code == 201, program.text
    made = client.post(f"/api/spd/programs/{program.json()['id']}/targets", headers=admin, json={
        "stage": "treat", "metric": "bp_sys", "target_high": 140, "followup_interval_days": 30})
    assert made.status_code == 201, made.text
    assert made.json()["followup_interval_days"] == 30
