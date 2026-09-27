"""模拟诊疗交卷后写着「练几次、进步多少都查得到」，页面上哪儿都查不到（P2-479，第八批扫描 V2-5）。

`GET /api/tcm-heritage/simulations/{case_id}/attempts` 一直在（全部作答留痕、每人最高分参与考核），前端一个调用都没有；
前端也不知道自己的用户编号，按 `user_id` 筛不出「我的」。作答行也不带交卷时刻。

修法：接口加 `mine=true`（只取当前账号自己的，给了就不看 `user_id`），作答行多带 `created_at`；模拟病例打开时与每次
交卷后，在下方列出「我的作答记录」（第几次、交卷时间、得分、结果，标题带次数与最高分）。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    case = client.post("/api/tcm-heritage/simulations", headers=admin, json={
        "title": "P2479 发热接诊", "category": "emergency", "scenario": "高热三天",
        "decision_points": [{"key": "s1", "question": "首选检查？", "options": ["血常规", "头颅CT"],
                             "answer": "血常规", "score": 100, "explain": "先分感染类型"}]}).json()
    trainee = client.post("/api/users", headers=admin, json={
        "username": "p2479_trainee", "password": "passw0rd1", "full_name": "P2479 学员", "role": "doctor"})
    assert trainee.status_code in (200, 201), trainee.text
    token = client.post("/api/auth/login", json={"username": "p2479_trainee", "password": "passw0rd1"}).json()
    return {"case": case["id"], "trainee": {"Authorization": f"Bearer {token['access_token']}"}}


def test_mine只取本人的作答_带交卷时刻(client, admin, world):
    url = f"/api/tcm-heritage/simulations/{world['case']}/attempts"
    for headers, answer in ((world["trainee"], "头颅CT"), (world["trainee"], "血常规"), (admin, "血常规")):
        assert client.post(url, headers=headers, json={"answers": {"s1": answer}}).status_code == 201
    mine = client.get(url, headers=world["trainee"], params={"mine": "true"})
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert [(a["attempt_no"], a["score"], a["passed"]) for a in body["attempts"]] == [(2, 100, True), (1, 0, False)]
    assert len({a["user_id"] for a in body["attempts"]}) == 1   # 修前 mine 不认，三条（含 admin 的）全回
    assert all(a["created_at"] for a in body["attempts"])        # 修前没有这个键
    assert [b["best_score"] for b in body["best_by_user"]] == [100]
    # 不带 mine 照旧是这个病例的全部作答
    assert len(client.get(url, headers=admin).json()["attempts"]) == 3


def test_打开病例与交卷后都列出我的作答记录():
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderTcmHeritage()")
    body = source[start:source.index("\nfunction renderCaseTable(", start)]
    assert "api(`/api/tcm-heritage/simulations/${sim.id}/attempts?mine=true`)" in body   # 修前没有一个调用
    draw = body[body.index("const drawSim = (sim) => {"):]
    assert draw.index("drawSimHistory(sim);") < draw.index('$("#sim-form").onsubmit')   # 打开病例就列
    submit = draw[draw.index('$("#sim-form").onsubmit'):]
    assert "drawSimHistory(sim);" in submit                                              # 交卷后刷新
    table = body[body.index('table(["第几次", "交卷时间", "得分", "结果"]'):]
    table = table[:table.index("</tr>`)")]
    for field in ("a.attempt_no", "a.created_at", "a.score", "a.passed"):
        assert field in table, field
