"""慢病风险评分卡的「趋势指标」原样印指标键（P2-1741，第五十一批扫描 AO4-5）。

慢病页 `core.js::renderChronic` 点「风险评分」画的卡片里，「趋势指标」原样印 `esc(r.metric)`：sbp、cat_score、
adherence_score——同一函数里已按病种目录建好 `metricNames`，同页随访史的「其他指标」早就按它换成了中文名（P2-478）；
不把编码印给人看是 P2-646 / P2-1512 的规矩。扫描按代码读出（`r3_history_level.py` 的风险回执里 `metric: 'sbp'`）。

修法：先取这份档案所属病种自己的那一项（后端 `_risk_metric` 取的就是它的第一个分级指标），病种目录里没有这一项的（规则清空后
回落兜底表的 sbp / glucose）按 `metricNames` 取名，再没有的原样印，一律 `esc()`。不只按键查 `metricNames`：它按键合并、
同一个键只留最后一个名字——预置糖尿病的空腹血糖挂了高、低两条（P2-119），按键查到的是「空腹血糖（低血糖）」，趋势却是按越高
越危算的（修复时实测）。页面原样拿到 node 里跑（`chronic_followup_pages`），风险回执取自真接口。
"""
import re
import shutil
from pathlib import Path

import pytest

from chronic_followup_pages import function_source, run

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 自建病种（P2-1739 起每个指标至少要有一个阈值）：另一个病种也用 sbp、名字不同（`metricNames` 里 sbp 取到的是它）；
#: 目录里哪儿都没有名字的指标原样印；名字里的标签要转义
CUSTOM = {
    "p21741_htn": [{"key": "sbp", "name": "收缩压（诊室）", "direction": "high", "level3": 160}],
    "p21741_raw": [{"key": "p21741_score", "direction": "high", "level3": 5}],
    "p21741_esc": [{"key": "p21741_tag", "name": "<i>危</i>", "direction": "high", "level3": 5}],
}
#: 档案的病种 → (风险回执的指标键, 卡片上印的趋势指标)
EXPECTED = {
    "hypertension": ("sbp", "收缩压"),   # 本病种自己那一项，不是 metricNames 里后来的「收缩压（诊室）」
    "diabetes": ("glucose", "空腹血糖"),   # 不是 metricNames 里后一条的「空腹血糖（低血糖）」
    "copd": ("cat_score", "CAT评分"),
    "severe_mental": ("adherence_score", "用药依从性评分"),
    "p21741_raw": ("p21741_score", "p21741_score"),
    "p21741_esc": ("p21741_tag", "&lt;i&gt;危&lt;/i&gt;"),
}
STEPS = """
  await renderChronic();
  const out = {};
  for (const [disease, id] of Object.entries(ARGS.params.ids)) {
    await click({ risk: String(id) });
    out[disease] = elements["#risk-box"].innerHTML;
  }
  return out;"""


def _shown(client, admin, ids: dict) -> dict:
    cards = run(client, admin, "admin", STEPS, {"ids": ids})
    return {disease: re.findall(r'<span class="k">趋势指标</span><b>([^<]*)</b>', html) for disease, html in cards.items()}


def test_卡片按名字印_不再原样印键():
    body = function_source((STATIC / "core.js").read_text(encoding="utf-8"), "async function renderChronic(")
    card = body[body.index('<span class="k">趋势指标</span>'):]
    card = card[:card.index("</div>")]
    assert "esc(r.metric)" not in card   # 修前原样印键
    assert "metricNames[r.metric] || r.metric" in card   # 病种目录里没有这一项的，同随访史「其他指标」按目录取名


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_趋势指标印病种目录里的名字(client, admin):
    for code, metrics in CUSTOM.items():
        created = client.post("/api/chronic/disease-types", headers=admin, json={
            "code": code, "name": f"{code} 病种", "level_rules": {"metrics": metrics}})
        assert created.status_code == 201, created.text
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21741 慢病院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for n, disease in enumerate(EXPECTED):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21741 患者{n}", "id_card": f"33010619650101741{n}"}).json()["id"]
        made = client.post("/api/chronic", headers=admin, json={
            "patient_id": patient, "disease": disease, "managed_by_org_id": org})
        assert made.status_code == 201, made.text
        ids[disease] = made.json()["id"]
    # 后端不动：风险回执里照旧是指标键
    keys = {disease: client.get(f"/api/chronic/{cid}/risk", headers=admin).json()["metric"] for disease, cid in ids.items()}
    assert keys == {disease: key for disease, (key, _name) in EXPECTED.items()}
    shown = _shown(client, admin, ids)
    assert shown == {disease: [name] for disease, (_key, name) in EXPECTED.items()}, shown   # 修前 sbp / glucose / cat_score …

    # 高血压的规则清空：后端回落兜底表的 sbp，病种目录里没有这一项，按 `metricNames` 取名
    htn = next(t for t in client.get("/api/chronic/disease-types", headers=admin).json() if t["code"] == "hypertension")
    cleared = client.patch(f"/api/chronic/disease-types/{htn['id']}", headers=admin, json={"level_rules": {}})
    assert cleared.status_code == 200 and cleared.json()["level_rules"] == {}, cleared.text
    assert client.get(f"/api/chronic/{ids['hypertension']}/risk", headers=admin).json()["metric"] == "sbp"
    assert _shown(client, admin, {"hypertension": ids["hypertension"]}) == {"hypertension": ["收缩压（诊室）"]}
