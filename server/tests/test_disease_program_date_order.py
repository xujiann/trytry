"""专病入组日、节点完成日不得晚于今天，节点完成日不得早于入组日；出组日可补录（P2-1543，第四十五批扫描 AI2-5）。

修前三个日子只查格式、不核先后、不核今天（扫描实测，修前代码）：入组日填 2026-12-01（将来）201；「首次评估」完成日填
2026-03-01（比入组早九个月）201；「疗效复评」完成日填 2027-06-30 201。9-01 已转院、今天补出组，请求里带的
`exited_at=2026-09-01` 被静默忽略（`ExitIn` 没有这个字段），出组日记成今天、比入组日还早。

修法照 `maternal.py` 的兄弟路径：已经发生的日子不得晚于今天（P2-1305 那一句），子记录不早于父事件（P2-1020 那一句）。
入组日、节点完成日不得晚于今天，节点完成日不得早于入组日；`ExitIn` 只增可选的 `exited_at`：缺省今天，不早于入组日、
不早于最晚的节点完成日、不晚于今天。都回 422、不落库。修前存进去的将来入组日 / 节点日不当下界（同 P2-940「将来的出生
日期按写坏处理」），免得这份病例从此记不了节点、出不了组。出组框补一格「出组日期」（留空按今天）。
"""
import shutil
from datetime import timedelta

import pytest

from chronic_followup_pages import run
from conftest import business_today

from app.database import SessionLocal
from app.models import DiseaseEnrollment, DiseasePathRecord


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21543 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    program = client.post("/api/disease-programs", headers=admin, json={
        "code": "P21543_STK", "name": "P21543 脑卒中", "path_nodes": [
            {"key": "assess", "name": "首次评估"}, {"key": "treat", "name": "康复治疗"},
            {"key": "review", "name": "疗效复评"}]})
    assert program.status_code == 201, program.text
    patients = []
    for n in range(9):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P21543 患者{n}", "id_card": f"33010619700101543{n}"})
        assert resp.status_code == 201, resp.text
        patients.append(resp.json()["id"])
    return {"org": org, "program": program.json()["id"], "patients": patients}


def _tomorrow() -> str:
    return (business_today() + timedelta(days=1)).isoformat()


def _enroll(client, admin, world, n, enrolled_at=None):
    body = {"patient_id": world["patients"][n], "org_id": world["org"]}
    if enrolled_at is not None:
        body["enrolled_at"] = enrolled_at
    return client.post(f"/api/disease-programs/{world['program']}/enrollments", headers=admin, json=body)


def _record(client, admin, enrollment_id, node_key, performed_at=None):
    body = {"node_key": node_key}
    if performed_at is not None:
        body["performed_at"] = performed_at
    return client.post(f"/api/disease-programs/enrollments/{enrollment_id}/records", headers=admin, json=body)


def _exit(client, admin, enrollment_id, **extra):
    return client.post(f"/api/disease-programs/enrollments/{enrollment_id}/exit", headers=admin,
                       json={"status": "exited", "exit_reason": "转上级医院", **extra})


def test_入组日不得晚于今天(client, admin, world):
    resp = _enroll(client, admin, world, 0, _tomorrow())
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": f"入组日期（{_tomorrow()}）不得晚于今天"}
    assert client.get(f"/api/disease-programs/enrollments?patient_id={world['patients'][0]}",
                      headers=admin).json() == []
    today = _enroll(client, admin, world, 0, business_today().isoformat())   # 今天照收
    assert today.status_code == 201, today.text


def test_节点完成日不得早于入组日_不得晚于今天(client, admin, world):
    enrollment = _enroll(client, admin, world, 1, "2026-08-01")
    assert enrollment.status_code == 201, enrollment.text
    eid = enrollment.json()["id"]
    early = _record(client, admin, eid, "assess", "2026-03-01")
    assert early.status_code == 422, early.text   # 修前 201：比入组早五个月
    assert early.json() == {"detail": "节点完成日期 2026-03-01 早于入组日期 2026-08-01"}
    future = _record(client, admin, eid, "review", _tomorrow())
    assert future.status_code == 422, future.text   # 修前 201
    assert future.json() == {"detail": f"节点完成日期（{_tomorrow()}）不得晚于今天"}
    with SessionLocal() as db:
        assert db.query(DiseasePathRecord).filter(DiseasePathRecord.enrollment_id == eid).count() == 0
    for performed_at in ("2026-08-01", "2026-08-15", None):   # 入组当天、之后、留空（按今天）照收
        ok = _record(client, admin, eid, "assess", performed_at)
        assert ok.status_code == 201, ok.text
    assert [r["performed_at"] for r in ok.json()["records"]] == ["2026-08-01", "2026-08-15",
                                                                 business_today().isoformat()]


def test_补录出组日_记成补录的那天(client, admin, world):
    eid = _enroll(client, admin, world, 2, "2026-08-01").json()["id"]
    assert _record(client, admin, eid, "assess", "2026-08-15").status_code == 201
    resp = _exit(client, admin, eid, exited_at="2026-09-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["exited_at"] == "2026-09-01"   # 修前被静默忽略，记成今天


def test_出组日不带_照旧是今天(client, admin, world):
    eid = _enroll(client, admin, world, 3, "2026-08-01").json()["id"]
    resp = _exit(client, admin, eid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["exited_at"] == business_today().isoformat()


@pytest.mark.parametrize(("n", "exited_at", "detail"), [
    (4, "2026-07-15", "出组日期 2026-07-15 早于入组日期 2026-08-01"),
    (5, "2026-08-10", "出组日期 2026-08-10 早于最晚的节点完成日期 2026-08-15"),
], ids=["早于入组日", "早于最晚的节点完成日"])
def test_补录的出组日早于入组日或最晚的节点日_422(client, admin, world, n, exited_at, detail):
    eid = _enroll(client, admin, world, n, "2026-08-01").json()["id"]
    for day in ("2026-08-15", "2026-08-05"):
        assert _record(client, admin, eid, "assess", day).status_code == 201
    resp = _exit(client, admin, eid, exited_at=exited_at)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": detail}
    after = client.get(f"/api/disease-programs/enrollments/{eid}", headers=admin).json()
    assert (after["status"], after["exited_at"]) == ("enrolled", "")


def test_补录的出组日晚于今天_422(client, admin, world):
    eid = _enroll(client, admin, world, 6, "2026-08-01").json()["id"]
    resp = _exit(client, admin, eid, exited_at=_tomorrow())
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": f"出组日期（{_tomorrow()}）不得晚于今天"}


def test_修前存进去的将来日子不当下界_照常记节点出组(client, admin, world):
    """修前入组日、节点日都能填将来，又都没有改的入口：拿它们当下界，这份病例从此记不了节点、出不了组。"""
    eid = _enroll(client, admin, world, 7, "2026-08-01").json()["id"]
    with SessionLocal() as db:
        db.get(DiseaseEnrollment, eid).enrolled_at = (business_today() + timedelta(days=57)).isoformat()
        db.add(DiseasePathRecord(enrollment_id=eid, node_key="review",
                                 performed_at=(business_today() + timedelta(days=200)).isoformat()))
        db.commit()
    assert _record(client, admin, eid, "assess").status_code == 201
    resp = _exit(client, admin, eid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["exited_at"] == business_today().isoformat()


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_出组框填得进出组日期(client, admin, world):
    """专病页「出组」原样跑在 node 里、请求转给真接口：框里多一格出组日期，填了按填的记。"""
    eid = _enroll(client, admin, world, 8, "2026-08-01").json()["id"]
    got = run(client, admin, "admin", r"""
await renderDiseasePrograms();
const pending = click({ dpexit: String(ARGS.params.eid) });
await tick();
const modal = lastModal();
await submitModal(modal, { status: "exited", exited_at: "2026-09-01", exit_reason: "转上级医院" });
await pending;
return { posts, msg: msgOf("#dp-msg") };
""", {"eid": eid}, send_writes=True, storage={"medplat_program": str(world["program"])})
    assert got["msg"] == "", got
    (post,) = got["posts"]
    assert post[:2] == ["POST", f"/api/disease-programs/enrollments/{eid}/exit"] and post[2]["exited_at"] == "2026-09-01"
    assert client.get(f"/api/disease-programs/enrollments/{eid}", headers=admin).json()["exited_at"] == "2026-09-01"
