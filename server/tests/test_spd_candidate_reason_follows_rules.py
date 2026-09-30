"""目标池「纳入依据」只在建行时算一次：复筛换了状态和命中规则，依据还是上一次的（P2-974，第二十七批「冗余的汇总列 / 派生字段
与明细不同步」扫描 G3-3）。

`_upsert_candidate` 的既有行分支改 `status` / `risk_level` / `matched_rules` / `screening_id`，不改 `reason`；新建行与就诊识别两处
都按命中规则的名称现算依据。页面「纳入依据」列只显示 `reason`：更正出生日期后复筛为疑似的，依据仍写「未成年人不纳入…」；
反过来复筛为排除的，依据仍写「确诊高血压」。

修法：三处共用 `service.candidate_reason`；复筛时依据仍是按上次命中规则自动算出来的，就跟着这次的换，手写的（手工改状态、
居民申请受理）不动，已纳管的不动。「纳入依据」该不该放复核说明另随 P2-382 定。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
MINOR = "未成年人不纳入成人高血压管理"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2974 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _patient(client, admin, org, name, id_card, birth):
    made = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card, "birth_date": birth})
    assert made.status_code in (200, 201), made.text
    pid = made.json()["id"]
    client.post("/api/encounters", headers=admin, json={
        "patient_id": pid, "org_id": org, "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    return pid


def _screen(client, admin, org, pid):
    resp = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": pid, "program_code": "hypertension", "org_id": org})
    assert resp.status_code == 201, resp.text
    return resp.json()["result"]


def _pool(pid):
    from app.spd.models import SpdCandidate

    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter_by(patient_id=pid, program_code="hypertension").one()
        return row.status, row.reason


def _set_birth(pid, birth):
    from app.models import Patient

    with SessionLocal() as db:
        db.get(Patient, pid).birth_date = birth
        db.commit()


def test_更正出生日期后复筛_依据跟着这次的命中规则(client, admin, org):
    minor = _patient(client, admin, org, "P2974 甲", "330106201103010075", "2011-03-01")
    assert _screen(client, admin, org, minor) == "excluded"
    assert _pool(minor) == ("excluded", MINOR)
    _set_birth(minor, "1961-03-01")   # 出生年份敲错了，更正后复筛
    assert _screen(client, admin, org, minor) == "suspect"
    status, reason = _pool(minor)
    assert status == "suspect" and MINOR not in reason and reason, reason   # 修前依据仍写「未成年人不纳入…」

    adult = _patient(client, admin, org, "P2974 乙", "330106196205050077", "1962-05-05")
    assert _screen(client, admin, org, adult) == "suspect"
    before = _pool(adult)[1]
    _set_birth(adult, "2012-05-05")
    assert _screen(client, admin, org, adult) == "excluded"
    assert _pool(adult) == ("excluded", MINOR), before   # 修前依据仍是上一次的「确诊高血压」一类


def test_手写的依据复筛不动(client, admin, org):
    from app.spd.models import SpdCandidate

    pid = _patient(client, admin, org, "P2974 丙", "330106196306060079", "1963-06-06")
    assert _screen(client, admin, org, pid) == "suspect"
    with SessionLocal() as db:   # 手工改状态时写了原因
        row = db.query(SpdCandidate).filter_by(patient_id=pid, program_code="hypertension").one()
        row.reason = "外院已确诊继发性高血压，转专科"
        db.commit()
    _screen(client, admin, org, pid)
    assert _pool(pid)[1] == "外院已确诊继发性高血压，转专科"


def test_三处共用一个算法():
    import inspect

    from app.spd import subscribers
    from app.spd.routers import population

    assert "candidate_reason(" in inspect.getsource(population._upsert_candidate)
    assert '"；".join' not in inspect.getsource(population._upsert_candidate)
    assert "candidate_reason(" in inspect.getsource(subscribers)
