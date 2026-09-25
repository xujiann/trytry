"""手工建任务时挂的纳管档案不看是不是这位患者、这个病种的：办完甲的随访任务，乙的随访日期被推后（P2-89）。

`create_task` 按 `enrollment_id` 取纳管档案，只查在不在。随访类任务办结时（`_finish_task`）会回写档案的「上次随访」
「下次随访」并给档案上的村医计分——挂错了档案，写的就是别人的档案。开发库实测（修前代码）：给甲建一条随访任务、挂上乙的
纳管档案，201；办结后乙的档案上次随访记成今天、下次随访推到一个月后——乙这一个月里没人随访，排期上却「刚随访过」。
病种对不上同理（高血压的任务挂到糖尿病档案上，推的是糖尿病的随访）。

修法：挂的档案必须是这位患者的（422「纳管档案不是这位患者的」）；任务写了病种的，须与档案同一病种（422）。
（挂别家机构的档案是否要判归属——县级团队下沉服务乡镇的患者——与服务团队跨机构同一个口径，见
`test_secondary_body_id_guard.BY_DESIGN`，不在本条。）
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P289 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    a = client.post("/api/patients", headers=admin, json={"name": "P289 甲", "id_card": "330127197511110289"}).json()["id"]
    b = client.post("/api/patients", headers=admin, json={"name": "P289 乙", "id_card": "330127197511110290"}).json()["id"]
    enrollments = {}
    for patient, program in ((a, "hypertension"), (b, "hypertension"), (a, "diabetes")):
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org})
        assert resp.status_code == 201, resp.text
        enrollments[(patient, program)] = resp.json()["id"]
    return {"org": org, "a": a, "b": b, "enrollments": enrollments}


def _task(client, admin, world, **kw):
    return client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["a"], "title": "P289 随访", "task_type": "followup", "org_id": world["org"], **kw})


def test_挂别人的纳管档案_422_别人的随访日期不动(client, admin, world):
    other = world["enrollments"][(world["b"], "hypertension")]
    resp = _task(client, admin, world, enrollment_id=other)
    assert resp.status_code == 422 and resp.json() == {"detail": "纳管档案不是这位患者的"}, resp.text   # 修前 201
    enrollment = client.get(f"{B}/enrollments/{other}", headers=admin).json()
    assert (enrollment["last_followup_at"], enrollment["next_followup_at"]) == ("", "")


def test_病种与档案对不上_422(client, admin, world):
    diabetes = world["enrollments"][(world["a"], "diabetes")]
    resp = _task(client, admin, world, enrollment_id=diabetes, program_code="hypertension")
    assert resp.status_code == 422 and resp.json() == {"detail": "纳管档案的病种与任务不一致"}, resp.text   # 修前 201


def test_挂自己的档案照旧_不写病种的随档案(client, admin, world):
    own = world["enrollments"][(world["a"], "hypertension")]
    for extra in ({}, {"program_code": "hypertension"}):
        resp = _task(client, admin, world, enrollment_id=own, **extra)
        assert resp.status_code == 201 and resp.json()["program_code"] == "hypertension", resp.text
