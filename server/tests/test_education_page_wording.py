"""远程医学教育页的两处文案对上数据（P2-1431，第四十二批「远程医学教育与培训考核」扫描 AF1-8）。

修前页面说明写「课程管理、培训考核（60分合格）、个人学分；…」——平台没有学分 / 学时字段，「我的学习记录」只列课程考试
（`/my-records` 读的是考试记录，点播不留个人记录）；课程「培训统计」卡片写「参训人次」——`trainees` 数的是考试记录行，而考试
记录每人每课一行（`uq_training_course_user`，重考取最高分改的是同一行），同一个人考三次还是 1，是人数。
修后改成「个人考核记录」「参训人数」，只改文案，接口不动。
"""
from pathlib import Path

from conftest import login
from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _render_education() -> str:
    src = strip_comments((STATIC / "pages-clinical.js").read_text(encoding="utf-8"))
    start = src.index("async function renderEducation(")
    return src[start:src.index("\n}\n", start) + 2]


def test_页面说明写个人考核记录_不写个人学分():
    body = _render_education()
    desc = body[body.index('$("#page-desc").textContent = '):]
    desc = desc[:desc.index(";\n")]
    assert "个人考核记录" in desc and "学分" not in desc, desc   # 修前「个人学分」
    assert "学时" not in body


def test_培训统计卡片写参训人数_不写人次():
    body = _render_education()
    assert '["参训人数", s.trainees]' in body and "参训人次" not in body   # 修前「参训人次」


def test_参训数的是考试人数_同一人考几次都只算一个(client, admin):
    """卡片的数取自 `/courses/{id}/stats` 的 trainees：一个人考三次、另一个人考一次，trainees 是 2——人数，不是人次；
    「我的学习记录」只有这门课的一行考试成绩，没有学分 / 学时。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21431 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = []
    for username in ("p21431_a", "p21431_b"):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": "doctor", "org_id": org})
        assert resp.status_code in (200, 201), resp.text
        heads.append(login(client, username, "pw123456"))
    course = client.post("/api/education/courses", headers=admin, json={"title": "P21431 课程"}).json()["id"]
    for head, score in ((heads[0], 40), (heads[0], 55), (heads[0], 85), (heads[1], 30)):
        assert client.post(f"/api/education/courses/{course}/exam", headers=head, json={"score": score}).status_code == 200
    stats = client.get(f"/api/education/courses/{course}/stats", headers=admin).json()
    assert (stats["trainees"], stats["passed"]) == (2, 1), stats
    mine = client.get("/api/education/my-records", headers=heads[0]).json()
    assert [(r["course_id"], r["score"]) for r in mine] == [(course, 85.0)]
    assert set(mine[0]) == {"course_id", "title", "score", "passed"}
