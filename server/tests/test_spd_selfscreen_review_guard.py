"""居民自查记录（不带机构）谁都能复核，顺手翻别家目标池里的那一行（P1-229，第二十六批 H2 扫描附带发现）。

`assert_org_writable` 对机构为空的记录放行（docstring：「由各接口自己定语义」），复核接口没定：居民自查落的筛查
机构为空，哪家机构的医生按编号都能复核——与这位患者毫无关系的也行，还按结论把别家机构目标池里这位患者翻成
目标 / 排除（别家筛出来、认领的那一行）。同样的事走「改池状态」「受理服务申请」都是 403。

修法：机构为空的筛查，与筛查清单同一口径——看得到这位患者的才能复核（留痕）；池里那一行归别家机构的，与改池状态、
受理申请同一口径，只有那家能动它。带机构的筛查照旧按机构守卫。自查记录归哪家复核（P2-637）另行待裁定。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdScreening
from conftest import login

B = "/api/spd"
ANSWERS = {"family": "是", "salt": "是", "overweight": "是", "smoke": "否", "drink": "否", "symptom": "是"}
PATIENT = {"name": "P1229 自查居民", "id_card": "330106195803020914", "gender": "男",
           "birth_date": "1958-03-02", "phone": "13900012290"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name in (("a", "P1229 甲卫生院"), ("b", "P1229 乙卫生院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
        created = client.post("/api/users", headers=admin, json={
            "username": f"p1229_{key}", "password": "passw0rd1", "full_name": f"P1229 {key}", "role": "doctor",
            "org_id": orgs[key]})
        assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json=PATIENT)
    assert patient.status_code in (200, 201), patient.text
    heads = {key: login(client, f"p1229_{key}", "passw0rd1") for key in ("a", "b")}
    # 乙院接诊过、筛出疑似：目标池那一行归乙院
    client.post("/api/encounters", headers=heads["b"], json={
        "patient_id": patient.json()["id"], "org_id": orgs["b"], "diagnosis_name": "头晕"})
    screened = client.post(f"{B}/screenings", headers=heads["b"], json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "scale_code": "scr_hypertension",
        "answers": ANSWERS})
    assert screened.status_code == 201 and screened.json()["result"] == "suspect", screened.text
    return {"orgs": orgs, "heads": heads, "patient": patient.json()["id"]}


@pytest.fixture(scope="module")
def self_screen(client, world):
    """居民在手机上自查一次：机构为空的一条疑似筛查。"""
    code = client.post("/api/portal/auth/sms/code", json={"phone": PATIENT["phone"]}).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": PATIENT["phone"], "code": code})
    assert resp.status_code == 200, resp.text
    ph = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post("/api/portal/auth/realname", headers=ph, json={"name": PATIENT["name"], "id_card": PATIENT["id_card"]})
    made = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": ANSWERS})
    assert made.status_code == 201 and made.json()["result"] == "suspect", made.text
    with SessionLocal() as db:
        assert db.get(SpdScreening, made.json()["id"]).org_id is None
    return made.json()["id"]


def _pool(world):
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter_by(patient_id=world["patient"], program_code="hypertension").one()
        return row.org_id, row.status


def _reviewed(screening_id):
    with SessionLocal() as db:
        return db.get(SpdScreening, screening_id).reviewed


def test_与患者无关的机构不能复核自查记录(client, world, self_screen):
    before = _pool(world)
    assert before == (world["orgs"]["b"], "suspect")
    resp = client.post(f"{B}/screenings/{self_screen}/review", headers=world["heads"]["a"],
                       json={"review_result": "excluded"})
    assert resp.status_code == 403, resp.text   # 修前 200：甲院把乙院池里的人翻成「排除」
    assert _pool(world) == before and _reviewed(self_screen) is False


def test_看得到患者_但池里那一行归别家的_也不能翻(client, world, self_screen):
    client.post("/api/encounters", headers=world["heads"]["a"], json={
        "patient_id": world["patient"], "org_id": world["orgs"]["a"], "diagnosis_name": "复查"})
    resp = client.post(f"{B}/screenings/{self_screen}/review", headers=world["heads"]["a"],
                       json={"review_result": "confirmed"})
    assert resp.status_code == 403, resp.text   # 与改池状态、受理申请同一口径
    assert _pool(world) == (world["orgs"]["b"], "suspect") and _reviewed(self_screen) is False


def test_池里那一行所属机构照常复核(client, world, self_screen):
    resp = client.post(f"{B}/screenings/{self_screen}/review", headers=world["heads"]["b"],
                       json={"review_result": "confirmed"})
    assert resp.status_code == 200, resp.text
    assert _pool(world) == (world["orgs"]["b"], "target") and _reviewed(self_screen) is True
