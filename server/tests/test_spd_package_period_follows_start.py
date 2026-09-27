"""服务包「有效期至」跟着纳管档案的服务起始日走（P2-576，第十一批「落库快照 vs 现查配置」扫描 Y3-5）。

有效期原先只在绑包那一刻按服务起始日算一次：
- 管理端建档表单与「调整纳管档案」对话框都不收服务起始日，界面建的档案绑包后「有效期至」一直是空的；
- 事后补填起始日，有效期照旧是空的；把起始日从 2026-09-01 改到 2027-01-01，有效期还是 2026-09-30。

修后：绑包时把服务包的有效天数与项目、次数、单价一起快照进绑定；改服务起始日时，在绑的服务包按快照天数重算
（包后来改了天数不影响已绑的）；解绑了的是历史不动；快照列上线前绑的天数未知，保持原值。两个入口补上起止日。
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import SessionLocal
from app.spd.models import SpdEnrollment, SpdPackageBinding

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2576 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": "p2576_dm", "name": "P2576 糖尿病", "category": "chronic",
        "stages": [{"key": "s1", "name": "一期"}]})
    assert program.status_code == 201, program.text
    package = client.post(f"{B}/service-packages", headers=admin, json={
        "code": "p2576_pkg", "name": "P2576 服务包", "program_code": "p2576_dm", "price": 200, "period_days": 30,
        "items": [{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}]})
    assert package.status_code == 201, package.text
    return {"org": org, "package": package.json()["id"], "seq": iter(range(10, 99))}


def _enroll(client, admin, world, **extra) -> int:
    n = next(world["seq"])
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2576 患者{n}", "id_card": f"3301271960010125{n:02d}"}).json()["id"]
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "p2576_dm", "org_id": world["org"], **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _bind(client, admin, world, eid: int) -> dict:
    resp = client.post(f"{B}/enrollments/{eid}/packages", headers=admin, json={"package_id": world["package"]})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_start(client, admin, eid: int, start: str):
    resp = client.patch(f"{B}/enrollments/{eid}", headers=admin, json={"service_start": start})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _period_end(binding_id: int) -> str:
    with SessionLocal() as db:
        return db.get(SpdPackageBinding, binding_id).period_end


def test_改起始日_有效期跟着挪(client, admin, world):
    eid = _enroll(client, admin, world, service_start="2026-09-01")
    binding = _bind(client, admin, world, eid)
    assert binding["period_end"] == "2026-09-30"   # 30 天的包到第 30 天为止（P2-547）
    assert _set_start(client, admin, eid, "2027-01-01")["service_start"] == "2027-01-01"
    assert _period_end(binding["id"]) == "2027-01-30"   # 修前仍是 2026-09-30


def test_没填起始日绑包_有效期空_事后补填按绑包时的天数算出(client, admin, world):
    eid = _enroll(client, admin, world)
    binding = _bind(client, admin, world, eid)
    assert binding["period_end"] == ""   # 不拿别的日子去猜
    _set_start(client, admin, eid, "2026-10-08")
    assert _period_end(binding["id"]) == "2026-11-06"   # 修前补填了照旧是空的


def test_包后来改了天数_已绑的按绑包那一刻的天数算(client, admin, world):
    eid = _enroll(client, admin, world, service_start="2026-09-01")
    binding = _bind(client, admin, world, eid)
    changed = client.patch(f"{B}/service-packages/{world['package']}", headers=admin, json={"period_days": 180})
    assert changed.status_code == 200, changed.text
    try:
        _set_start(client, admin, eid, "2026-10-01")
        assert _period_end(binding["id"]) == "2026-10-30"   # 仍是 30 天，不是包现在的 180 天
    finally:
        client.patch(f"{B}/service-packages/{world['package']}", headers=admin, json={"period_days": 30})


def test_清空起始日_有效期随之为空(client, admin, world):
    eid = _enroll(client, admin, world, service_start="2026-09-01")
    binding = _bind(client, admin, world, eid)
    _set_start(client, admin, eid, "")
    assert _period_end(binding["id"]) == ""


def test_解绑的是历史_快照列上线前绑的天数未知_都不动(client, admin, world):
    eid = _enroll(client, admin, world, service_start="2026-09-01")
    old = _bind(client, admin, world, eid)
    unbound = client.post(f"{B}/package-bindings/{old['id']}/unbind", headers=admin)
    assert unbound.status_code == 200, unbound.text
    with SessionLocal() as db:   # 本列上线前绑的：period_days 为 0
        legacy = SpdPackageBinding(enrollment_id=eid, package_id=world["package"], items=[], status="bound",
                                   period_end="2026-10-01")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    _set_start(client, admin, eid, "2027-03-01")
    assert _period_end(old["id"]) == "2026-09-30"
    assert _period_end(legacy_id) == "2026-10-01"


def test_起始日按读到的旧值条件写_被别人先改了的409(client, admin, world):
    from app.spd.routers.population import _follow_service_start

    eid = _enroll(client, admin, world, service_start="2026-09-01")
    binding = _bind(client, admin, world, eid)
    with SessionLocal() as db:
        enrollment = db.get(SpdEnrollment, eid)
        enrollment.service_start = "2026-12-01"
        with pytest.raises(HTTPException) as err:
            # 这一路读到的旧值是 08-01，库里已是 09-01：另一路先改了
            _follow_service_start(db, enrollment, "2026-08-01")
        assert err.value.status_code == 409
    with SessionLocal() as db:
        assert db.get(SpdEnrollment, eid).service_start == "2026-09-01"
    assert _period_end(binding["id"]) == "2026-09-30"


def test_建档表单与调整对话框都收服务起止日():
    source = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    form = source[source.index('<form class="inline" id="spd-enroll-form">'):]
    form = form[:form.index("</form>")]
    for name in ("service_start", "service_end"):
        assert f'name="{name}"' in form, name   # 修前建档表单不收
    dialog = source[source.index('spdModal("调整纳管档案'):]
    dialog = dialog[:dialog.index("postAction(")]
    for name in ("service_start", "service_end"):
        assert f'name: "{name}"' in dialog, name   # 修前对话框不收
    assert 'for (const k of ["next_followup_at", "service_start", "service_end"]) if (form[k]) body[k] = form[k];' in dialog
