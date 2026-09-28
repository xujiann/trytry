"""量表得分没落进任何评分分段就是「未分级」，筛查登记与居民自查不再补成「低危」（P2-689，第十七批「缺失 vs 零」扫描 U1-4）。

`score_scale` 没有分段命中时风险等级为空串。量表评估照实记空串、页面写「未分级」、不回写档案；筛查登记
（`graded["risk_level"] or "low"`）与居民自查（`str(graded["risk_level"] or "low")`）却补成「低危」。量表构建器允许
分段之间留缺口、上限封顶、选项分值带小数：分段 0–1 低 / 2–3 中 / 4–4 高，一个 1.5 分的选项、或者答出 4.5 分——
居民自查告诉他「低危」、不提示申请服务；医护筛查登记记「低危」。最高分被判成最低风险。
"""
from pathlib import Path

import pytest

from app.spd.rules import score_scale
from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PATIENT = {"name": "P2689 自查居民", "id_card": "330106197106060899", "gender": "女",
           "birth_date": "1971-06-06", "phone": "13900006890"}
ITEMS = [{"key": "q1", "title": "头晕发作", "type": "single",
          "options": [{"label": "从不", "score": 0}, {"label": "偶尔", "score": 1.5}, {"label": "每天", "score": 4.5}]}]
SCORING = {"ranges": [{"min": 0, "max": 1, "risk": "low", "advice": "保持健康生活方式"},
                      {"min": 2, "max": 3, "risk": "mid", "advice": "建议复核血压"},
                      {"min": 4, "max": 4, "risk": "high", "advice": "建议尽快评估"}]}
NOTE = "没有落在量表的任何评分分段里，未能按量表分级"


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post("/api/organizations", headers=h, json={
        "name": "P2689 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=h, json=PATIENT)
    assert patient.status_code in (200, 201), patient.text
    scale = client.post("/api/spd/scales", headers=h, json={
        "code": "scr_p2689", "name": "P2689 分段有缺口的筛查量表", "category": "screen",
        "program_code": "hypertension", "items": ITEMS, "scoring": SCORING})
    assert scale.status_code == 201, scale.text
    assert client.post(f"/api/spd/scales/{scale.json()['id']}/publish", headers=h).status_code == 200
    return {"org": org, "patient": patient.json()["id"]}


@pytest.fixture(scope="module")
def ph(client, base):
    """居民令牌：短信验证码登录 + 实名绑定（与既有居民端用例同一取法）。"""
    code = client.post("/api/portal/auth/sms/code", json={"phone": PATIENT["phone"]}).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": PATIENT["phone"], "code": code})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post("/api/portal/auth/realname", headers=headers,
                json={"name": PATIENT["name"], "id_card": PATIENT["id_card"]})
    return headers


def test_评分_没落进分段就是未分级并写明原因():
    gap = score_scale(ITEMS, {"q1": "偶尔"}, SCORING)
    assert (gap["score"], gap["risk_level"]) == (1.5, "")
    assert NOTE in gap["advice"] and "1.5" in gap["advice"]
    top = score_scale(ITEMS, {"q1": "每天"}, SCORING)
    assert (top["risk_level"], NOTE in top["advice"]) == ("", True)
    inside = score_scale(ITEMS, {"q1": "从不"}, SCORING)                     # 落进分段的照旧
    assert (inside["risk_level"], inside["advice"]) == ("low", "保持健康生活方式")
    assert score_scale(ITEMS, {"q1": "偶尔"}, {})["advice"] == ""            # 没设分段的量表不多话


def test_筛查登记_不补成低危(client, h, base):
    resp = client.post("/api/spd/screenings", headers=h, json={
        "patient_id": base["patient"], "program_code": "hypertension", "source": "opportunistic",
        "org_id": base["org"], "scale_code": "scr_p2689", "answers": {"q1": "每天"}})
    assert resp.status_code == 201, resp.text
    assert resp.json()["risk_level"] == ""   # 修前 low：最高分落进缺口记成低危
    assert NOTE in resp.json()["advice"]


def test_居民自查_不告诉他低危(client, ph, base):
    resp = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": "scr_p2689", "answers": {"q1": "每天"}})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["risk_level"] == "" and NOTE in body["advice"]   # 修前 low、advice 为空
    assert body["can_apply"] is False


def test_评估_照旧记未分级且写明原因(client, h, base):
    resp = client.post("/api/spd/assessments", headers=h, json={
        "patient_id": base["patient"], "scale_code": "scr_p2689", "answers": {"q1": "偶尔"},
        "program_code": "hypertension"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["risk_level"] == "" and NOTE in resp.json()["advice"]


def test_两端页面把空串写成未分级():
    m_js = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    assert 'r.risk_level ? (SPD_RISK_TAGS[r.risk_level] || [r.risk_level])[0] : "未分级"' in m_js   # 修前「风险等级：。」
    spd_js = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    assert 'spdTag(SPD_RISK, s.risk_level || "未分级")' in spd_js
