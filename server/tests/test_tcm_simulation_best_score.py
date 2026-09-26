"""模拟诊疗「每人最高分」与「第几次作答」按这个病例的全部作答算，不按取回的最近 500 条算（P2-149）。

作答记录接口写「取最高分参与考核，但全部留痕——『第几次才做对』本身就是教学反馈」；实现只在最近 500 条作答里
算最高分与作答序号。一个病例练的人一多，某人最好的那次早于最近 500 条，他在「最高分」里就降了分或整个消失——
考核取的正是这个数；作答序号也从窗口起点重新数起。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    case = client.post("/api/tcm-heritage/simulations", headers=admin, json={
        "title": "P2149 胸痛接诊", "category": "emergency", "scenario": "突发胸痛",
        "decision_points": [{"key": "s1", "question": "首选检查？", "options": ["心电图", "腹部B超"],
                             "answer": "心电图", "score": 100, "explain": "排除急性冠脉综合征"}]}).json()
    trainee = client.post("/api/users", headers=admin, json={
        "username": "p2149_trainee", "password": "passw0rd1", "full_name": "P2149 学员", "role": "doctor"})
    assert trainee.status_code in (200, 201), trainee.text
    return {"case": case["id"], "trainee": trainee.json()["id"]}


def test_最好的那次早于最近500条_最高分照样算上(client, admin, world):
    from app.database import SessionLocal
    from app.models import SimulationAttempt, User

    best = client.post(f"/api/tcm-heritage/simulations/{world['case']}/attempts", headers=admin,
                       json={"answers": {"s1": "心电图"}})
    assert best.status_code == 201 and best.json()["score"] == 100
    with SessionLocal() as db:   # 之后另一名学员练了 502 次、都没做对
        admin_id = db.query(User).filter(User.username == "admin").one().id
        db.add_all([SimulationAttempt(case_id=world["case"], user_id=world["trainee"], answers={}, score=0,
                                      passed=False) for _ in range(502)])
        db.commit()

    body = client.get(f"/api/tcm-heritage/simulations/{world['case']}/attempts", headers=admin).json()
    assert len(body["attempts"]) == 500
    # 修前 [(学员, 0)]：admin 那次满分落在窗口外，整个人从「最高分」里消失
    assert [(b["user_id"], b["best_score"]) for b in body["best_by_user"]] == [(world["trainee"], 0), (admin_id, 100)]
    assert body["attempts"][0]["attempt_no"] == 502   # 修前 500：序号从窗口起点数起
