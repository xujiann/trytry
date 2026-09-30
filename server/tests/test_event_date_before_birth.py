"""出生日期不当下界用：接种、新生儿访视、新生儿筛查、死亡日期都能早于出生日期；儿童档案出生日期能填将来（P2-940，
第二十六批「人口学属性与业务对象的适配」扫描 H2-7）。

P2-713 只管了患者建档与更正的出生日期不得晚于今天。2026-05-01 出生的婴儿登记 2025-05-01 的乙肝第 1 剂 201（接种登记
没有逆操作，改不回来）；早于出生的新生儿访视 201；早于出生的异常新筛 201、孩子被标成高危儿；儿童建档出生日期填
2027-03-01 照收；1940 年生的人签发 1930 年死亡的证明 201，死因报告卡照导出去网报。

修法：有出生日期时四类事件日期早于出生日期一律 422（`datetypes.before_birth_problem`，出生日期按存量写法读）。
当天的照收；出生日期空着的不拦。儿童建档出生日期晚于今天的拦截没有一起做：既有用例按将来的时间线建儿童档案，
要拦须把那些时间线一并挪回今天以前，另行处理。
"""
import pytest

from app.database import SessionLocal
from app.models import ChildRecord

INFANT = {"name": "P2940 婴儿", "id_card": "330106202605010099", "gender": "男", "birth_date": "2026-05-01"}
ELDER = {"name": "P2940 老人", "id_card": "330106194003030087", "gender": "女", "birth_date": "1940-03-03"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2940 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for key, person in (("infant", INFANT), ("elder", ELDER)):
        made = client.post("/api/patients", headers=admin, json=person)
        assert made.status_code in (200, 201), made.text
        ids[key] = made.json()["id"]
    child = client.post("/api/maternal/children", headers=admin, json={
        "name": "P2940 新生儿", "gender": "女", "birth_date": "2026-05-01"})
    assert child.status_code == 201, child.text
    return {"org": org, "child": child.json()["id"], **ids}


def _vaccinate(client, admin, world, day):
    return client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["infant"], "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗", "dose_no": 1,
        "vaccinated_date": day, "org_id": world["org"]})


def test_接种日期早于出生日期_422_当天照收(client, admin, world):
    early = _vaccinate(client, admin, world, "2025-05-01")
    assert early.status_code == 422, early.text   # 修前 201
    assert "早于出生日期（2026-05-01）" in early.json()["detail"]
    assert _vaccinate(client, admin, world, "2026-05-01").status_code == 201


def test_新生儿访视与筛查早于出生_422_不标高危(client, admin, world):
    child = world["child"]
    visit = client.post(f"/api/maternal/children/{child}/visits", headers=admin, json={
        "visit_type": "newborn", "visit_date": "2026-03-01"})
    assert visit.status_code == 422, visit.text   # 修前 201
    ok = client.post(f"/api/maternal/children/{child}/visits", headers=admin, json={
        "visit_type": "newborn", "visit_date": "2026-05-08"})
    assert ok.status_code == 201, ok.text
    blank = client.post(f"/api/maternal/children/{child}/visits", headers=admin, json={"visit_type": "checkup"})
    assert blank.status_code == 201, blank.text   # 没写日期的不拦
    screen = client.post(f"/api/maternal/children/{child}/screenings", headers=admin, json={
        "item": "hearing", "result": "abnormal", "screen_date": "2026-04-01"})
    assert screen.status_code == 422, screen.text   # 修前 201 且标高危
    with SessionLocal() as db:
        assert db.get(ChildRecord, child).high_risk is False


def test_死亡日期早于出生日期_不签(client, admin, world):
    def issue(day):
        return client.post("/api/certs", headers=admin, json={
            "cert_type": "death", "name": ELDER["name"], "gender": "女", "event_date": day,
            "detail": "心力衰竭", "org_id": world["org"], "patient_id": world["elder"]})
    early = issue("1930-03-03")
    assert early.status_code == 422, early.text   # 修前 201
    assert "死亡日期" in early.json()["detail"]
    assert issue("2026-09-01").status_code == 201
