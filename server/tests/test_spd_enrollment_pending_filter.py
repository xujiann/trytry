"""团队工作台「待评估 / 待定目标 / 待建路径」点得进档案清单：清单按工作台同一句判据筛（P2-1316，第三十八批扫描 AB1-3 之一）。

团队工作台（成员 / 个案管理师 / 专家端）报着「待评估 2 / 待定目标 1 / 待建路径 3」，可档案清单 `list_enrollments` 没有对应的
筛选：`stage=` 空串被 `value != ""` 吞掉等于不筛（三份全回来），评估、路径更无从表达；页面上这三格也只是数字——没有一处能
列出是哪几份档案。

修后三条判据抽进 `service`（`enrollment_unassessed` / `enrollment_unstaged` / `enrollment_pathless`，照 P2-825 的
`task_unclaimed`），工作台计数与清单的 `pending=assess|target|path` 共用；工作台三个视角各自的「我的」（`team_view_scope`）
也共用，清单 `team_role=` 认它——管理端页面拿不到本人的账号编号，卡片点进清单只能让服务端按同一个视角认「我」。
`stage=` 空串照旧是不筛（与 `status=` 同一个约定）。页面三格可点，带着视角与判据跳到「筛查建档与纳管」的档案清单。
"""
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
ROLES = ("member", "case_manager", "expert")
PENDING = {"assess": "pending_assess", "target": "pending_target", "path": "pending_path"}


@pytest.fixture(scope="module")
def world(client, admin):
    """同一位医生在三个视角下各管一批：主管医生（成员端）e1 e2 e3 e5 e6、个案管理师 e1 e2、所在团队 e1 e3；e4 是别人的。

    - e1 自建病种（没配阶段，`stage` 落空串）：没评估、没路径——三类都算；
    - e2 高血压：没评估、路径执行中——只算待评估；
    - e3 高血压：做过高血压评估、路径已取消（取消的不算有路径）——只算待建路径；
    - e5 e3 那位患者的糖尿病：不带病种按人算已评估，带 `program_code=diabetes` 按病种算待评估（P2-851 / P2-139）；
    - e6 糖尿病、做过糖尿病评估：带不带病种都不算待评估。
    """
    from app.spd.models import (SpdAssessment, SpdPathInstance, SpdPathTemplate, SpdProgram, SpdScale, SpdTeam,
                                SpdTeamMember)

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21316 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    users = {}
    for key in ("doc", "other"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p21316_{key}", "password": "passw0rd1", "role": "doctor", "org_id": org,
            "full_name": f"P21316 {key}"})
        assert made.status_code == 201, made.text
        users[key] = made.json()["id"]
    made = client.post(f"{B}/programs", headers=admin, json={"code": "p21316", "name": "P21316 未配阶段", "category": "chronic"})
    assert made.status_code == 201, made.text
    with SessionLocal() as db:
        mine, theirs = SpdTeam(name="P21316 甲组", org_id=org), SpdTeam(name="P21316 乙组", org_id=org)
        db.add_all([mine, theirs])
        db.flush()
        db.add(SpdTeamMember(team_id=mine.id, user_id=users["doc"], member_role="expert"))
        db.commit()
        teams = {"mine": mine.id, "theirs": theirs.id}
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P21316 患者{n}", "id_card": f"33010619700101{1316 + n:04d}"}).json()["id"] for n in range(5)]
    enroll = {}
    for key, pid, program, doctor, manager, team in (
            ("e1", patients[0], "p21316", "doc", "doc", "mine"),
            ("e2", patients[1], "hypertension", "doc", "doc", "theirs"),
            ("e3", patients[2], "hypertension", "doc", None, "mine"),
            ("e4", patients[3], "hypertension", "other", "other", "theirs"),
            ("e5", patients[2], "diabetes", "doc", None, None),
            ("e6", patients[4], "diabetes", "doc", None, None)):
        made = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": pid, "program_code": program, "org_id": org, "doctor_user_id": users[doctor],
            "manager_user_id": users[manager] if manager else None, "team_id": teams[team] if team else None})
        assert made.status_code == 201, made.text
        enroll[key] = made.json()
    assert enroll["e1"]["stage"] == "" and enroll["e2"]["stage"] != ""   # 前提：没配阶段的病种落空串
    with SessionLocal() as db:
        scale = db.query(SpdScale).order_by(SpdScale.id).first()
        program_id = db.query(SpdProgram.id).filter(SpdProgram.code == "hypertension").scalar()
        template = SpdPathTemplate(program_id=program_id, code="P21316_T", name="P21316 路径")
        db.add(template)
        db.flush()
        db.add_all([
            SpdAssessment(patient_id=patients[2], scale_id=scale.id, scale_code=scale.code,
                          program_code="hypertension", score=1, risk_level="low"),
            SpdAssessment(patient_id=patients[4], scale_id=scale.id, scale_code=scale.code,
                          program_code="diabetes", score=1, risk_level="low"),
            SpdPathInstance(enrollment_id=enroll["e2"]["id"], template_id=template.id, status="running"),
            SpdPathInstance(enrollment_id=enroll["e3"]["id"], template_id=template.id, status="cancelled"),
        ])
        db.commit()
    return {"h": login(client, "p21316_doc", "passw0rd1"), "ids": {k: v["id"] for k, v in enroll.items()}}


def _plans(client, world, role, **params):
    resp = client.get(f"{B}/workbench/team", headers=world["h"], params={"role": role, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["plans"]


def _listed(client, world, **params):
    resp = client.get(f"{B}/enrollments", headers=world["h"], params={"limit": 100, **params})
    assert resp.status_code == 200, resp.text
    return int(resp.headers["X-Total-Count"]), {r["id"] for r in resp.json()}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("pending", sorted(PENDING))
def test_工作台每一格都等于同视角同判据清单的总数(client, world, role, pending):
    total, _ = _listed(client, world, team_role=role, pending=pending)
    assert total == _plans(client, world, role)[PENDING[pending]]   # 修前清单不认这两个参数：总数是全部 6 份


def test_成员端三格列出的正是那几份档案(client, world):
    ids = world["ids"]
    assert _listed(client, world, team_role="member", pending="assess")[1] == {ids["e1"], ids["e2"]}
    assert _listed(client, world, team_role="member", pending="target")[1] == {ids["e1"]}
    assert _listed(client, world, team_role="member", pending="path")[1] == {ids["e1"], ids["e3"], ids["e5"], ids["e6"]}
    assert _listed(client, world, team_role="expert", pending="path")[1] == {ids["e1"], ids["e3"]}   # 我所在的团队


def test_待评估带病种按病种判_与工作台同一句(client, world):
    total, rows = _listed(client, world, team_role="member", pending="assess", program_code="diabetes")
    assert rows == {world["ids"]["e5"]}   # 修前 {e5, e6}；e5 做的是高血压评估：糖尿病档案带病种时仍算待评估
    assert total == _plans(client, world, "member", program_code="diabetes")["pending_assess"] == 1


def test_取值不在范围内的_422(client, world):
    for params in ({"pending": "assessed"}, {"pending": "stage"}, {"team_role": "doctor"}, {"team_role": "mine"}):
        resp = client.get(f"{B}/enrollments", headers=world["h"], params=params)
        assert resp.status_code == 422, (params, resp.text)


def test_stage空串照旧是不筛(client, world):
    total, _ = _listed(client, world, team_role="member", stage="")
    assert total == 5   # 与 `status=` 同一个约定：空串不筛（「待定目标」走 pending=target）


def test_团队工作台三格可点_带视角与判据跳到档案清单():
    start = PAGE.index("async function renderSpdTeam()")
    team = PAGE[start:PAGE.index("\nasync function ", start + 10)]
    for label, key in (("待评估", "assess"), ("待定目标", "target"), ("待建路径", "path")):
        assert f'["{label}", wb.plans.{PENDING[key]}, false, "{key}"]' in team, label   # 修前只是数字
    assert 'spdEnrollJump = { team_role: role, pending: jump.dataset.spdJump };' in team
    assert 'nav("spdpatients")' in team
    cards = PAGE[PAGE.index("function spdCards(items)"):PAGE.index("\n}\n", PAGE.index("function spdCards(items)"))]
    assert 'data-spd-jump="${esc(jump)}"' in cards


def test_档案清单筛选栏有视角与进度_首屏按带来的条件查():
    start = PAGE.index('<form class="inline" id="spd-enroll-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="team_role">' in form and '<select name="pending">' in form
    for value in ("member", "case_manager", "expert", "assess", "target", "path"):
        assert f'<option value="{value}">' in form, value
    start = PAGE.index("async function renderSpdPatients()")
    body = PAGE[start:PAGE.index("\nasync function ", start + 10)]
    assert "const jump = spdEnrollJump;" in body and "spdEnrollJump = null;" in body   # 带过来的条件只用一次
    assert "drawEnrollments(jump ? formJson($(\"#spd-enroll-filter\")) : undefined)" in body
