"""服务团队的「服务病种」参与分发与建档（P2-1340，第三十九批「辖区与归属」扫描 AC2-9）。

团队配置把「管这个病种的团队」定义为服务病种（`program_codes`）含它（团队清单按 `program_code=` 筛即按它挑），分发目标患者、
建档 / 改档挂团队原先只看团队在不在、停没停用：糖尿病目标患者照样分给只服务高血压的团队（扫描实测 200）、糖尿病档案照样
挂上去（201）。与 P2-98 / P2-99（被引用的对象挂在病种上，须是这个病种的）同一族。

修后服务病种非空且不含该病种的 422，并点名团队与病种；服务病种为空的团队照旧不限（空 = 不限）。目录接口的团队带上
`program_codes`，签约建档表单的团队下拉按病种联动（空的照列）。
"""
from pathlib import Path

import pytest

B = "/api/spd"
SPD_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21340 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    teams = {}
    for key, codes in (("htn", ["hypertension"]), ("open", [])):
        made = client.post(f"{B}/teams", headers=admin, json={
            "name": f"P21340 {key}团队", "org_id": org, "level": "township", "program_codes": codes})
        assert made.status_code == 201, made.text
        teams[key] = made.json()["id"]
    return {"org": org, "teams": teams}


_seq = iter(range(10, 99))


def _patient(client, admin):
    made = client.post("/api/patients", headers=admin, json={
        "name": "P21340 患者", "id_card": f"3301021965050513{next(_seq)}"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def _candidate(client, admin, world, program_code, claimed=False):
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.models import SpdCandidate

    with SessionLocal() as db:
        row = SpdCandidate(patient_id=_patient(client, admin), program_code=program_code, status="target",
                           source="screening", org_id=world["org"], risk_level="high", matched_rules=[],
                           reason="用例夹具", claimed_at=now_naive() if claimed else None)
        db.add(row)
        db.commit()
        return row.id


def _team_of(candidate_id):
    from app.database import SessionLocal
    from app.models import SpdCandidate

    with SessionLocal() as db:
        return db.get(SpdCandidate, candidate_id).team_id


def _distribute(client, admin, ids, team):
    return client.post(f"{B}/candidates/distribute", headers=admin, json={"candidate_ids": ids, "team_id": team})


def test_分发_不服务该病种的团队422并点名_整批不动(client, admin, world):
    teams = world["teams"]
    diabetes, hypertension = (_candidate(client, admin, world, code) for code in ("diabetes", "hypertension"))
    refused = _distribute(client, admin, [diabetes, hypertension], teams["htn"])
    assert refused.status_code == 422, refused.text   # 修前 200、糖尿病那条照样分进高血压团队
    assert "P21340 htn团队" in refused.json()["detail"] and "糖尿病" in refused.json()["detail"], refused.text
    assert (_team_of(diabetes), _team_of(hypertension)) == (None, None)   # 整批拒收，不留半成品
    ok = _distribute(client, admin, [hypertension], teams["htn"])
    assert ok.status_code == 200 and ok.json()["distributed"] == 1, ok.text


def test_分发_服务病种为空的团队照旧不限(client, admin, world):
    diabetes = _candidate(client, admin, world, "diabetes")
    ok = _distribute(client, admin, [diabetes], world["teams"]["open"])
    assert ok.status_code == 200 and ok.json()["distributed"] == 1, ok.text
    assert _team_of(diabetes) == world["teams"]["open"]


def test_分发_已被认领的不动也不拦(client, admin, world):
    claimed = _candidate(client, admin, world, "diabetes", claimed=True)
    hypertension = _candidate(client, admin, world, "hypertension")
    ok = _distribute(client, admin, [claimed, hypertension], world["teams"]["htn"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["distributed"] == 1 and ok.json()["skipped_claimed"] == [claimed]
    assert (_team_of(claimed), _team_of(hypertension)) == (None, world["teams"]["htn"])


def _enroll(client, admin, world, program_code, team):
    return client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": _patient(client, admin), "program_code": program_code, "org_id": world["org"], "team_id": team})


def test_建档改档_不服务该病种的团队422并点名_空服务病种的照旧(client, admin, world):
    teams = world["teams"]
    refused = _enroll(client, admin, world, "diabetes", teams["htn"])
    assert refused.status_code == 422, refused.text   # 修前 201，糖尿病档案挂在高血压团队名下
    assert "P21340 htn团队" in refused.json()["detail"] and "糖尿病" in refused.json()["detail"], refused.text
    assert _enroll(client, admin, world, "hypertension", teams["htn"]).status_code == 201

    open_made = _enroll(client, admin, world, "diabetes", teams["open"])
    assert open_made.status_code == 201, open_made.text
    enrollment = open_made.json()["id"]
    moved = client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"team_id": teams["htn"]})
    assert moved.status_code == 422, moved.text       # 修前 200
    assert "P21340 htn团队" in moved.json()["detail"] and "糖尿病" in moved.json()["detail"], moved.text


def test_改档_团队没换的不再按服务病种挡(client, admin, world):
    """与改档其余引用同一句（与现值相同的不再查）：团队后来改了服务病种，整份回传档案、实际只改风险分层的不该被挡住。"""
    team = client.post(f"{B}/teams", headers=admin, json={
        "name": "P21340 后改团队", "org_id": world["org"], "level": "township"}).json()["id"]
    made = _enroll(client, admin, world, "diabetes", team)
    assert made.status_code == 201, made.text
    changed = client.patch(f"{B}/teams/{team}", headers=admin, json={"program_codes": ["hypertension"]})
    assert changed.status_code == 200, changed.text
    kept = client.patch(f"{B}/enrollments/{made.json()['id']}", headers=admin,
                        json={"team_id": team, "risk_level": "high"})
    assert kept.status_code == 200, kept.text


def test_目录接口的团队带服务病种(client, admin, world):
    teams = {t["id"]: t for t in client.get(f"{B}/catalog", headers=admin).json()["teams"]}
    assert teams[world["teams"]["htn"]]["program_codes"] == ["hypertension"]   # 修前没有这个键
    assert teams[world["teams"]["open"]]["program_codes"] == []


def test_签约建档表单的团队下拉按病种联动_空服务病种的照列():
    src = SPD_JS.read_text(encoding="utf-8")
    start = src.index('<form class="inline" id="spd-enroll-form">')
    form = src[start:src.index("</form>", start)]
    assert "catalog.teams.map" not in form   # 修前直接列全县启用的团队
    sync = src[src.index("const syncEnrollTeams = () => {"):]
    sync = sync[:sync.index("\n  };")]
    assert "!(t.program_codes || []).length || t.program_codes.includes(program)" in sync
    assert '$("#spd-enroll-form select[name=program_code]").onchange = syncEnrollTeams;' in src
