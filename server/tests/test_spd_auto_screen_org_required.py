"""不挂机构的全域账号点「按规则自动识别」、机构框留空：按「机构为空」去扫就诊，恒 0 人、200、不报原因（P2-1140，
第三十三批扫描 A1-2）。

`auto_screen` 取 `org_id = body.org_id 或 user.org_id`，接着按 `Encounter.org_id == org_id` 扫就诊。管理层、admin 一般
不挂机构，页面占位又写「机构ID(留空取本机构)」——留空提交，查的是「机构为空的就诊」，而就诊记录必挂机构（列非空），
于是恒得 `scanned 0`、200：页面弹「扫描 0 人」，目标池空着，操作的人以为本县没有目标人群。修前实测（scan33 a1/r5）：
本机构已有 3 条 I10 就诊，管理层留空 `200 {'scanned': 0…}`、admin 同样，目标池 0 条；填上机构号 `scanned 3, suspect 3`。
兄弟接口「按患者特征自动匹配随访」遇同一情形回 422「本账号没有所属机构」（P2-92）。

修法：与 `followup.auto_match_plans` 同一句 422；docstring 里「全域跑批由 admin 走定时任务」改成如实的说法（任务注册表
里没有这一项）。挂了机构的账号留空照旧取本机构，填了机构照常扫。
"""
import pytest
from conftest import login

from app.database import SessionLocal

B = "/api/spd"
DETAIL = "请指定按哪家机构的就诊记录识别（本账号没有所属机构）"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1140 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, role, org_id in (("p1140_dir", "director", None), ("p1140_doc", "doctor", org)):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org_id})
        assert made.status_code in (200, 201), made.text
    for i, id_card in enumerate(("330106195001011140", "330106195502021141", "330106196003031142")):
        made = client.post("/api/patients", headers=admin, json={
            "name": f"P1140 患者{i}", "id_card": id_card, "birth_date": f"{1950 + 5 * i}-01-01"})
        assert made.status_code in (200, 201), made.text
        visit = client.post("/api/encounters", headers=admin, json={
            "patient_id": made.json()["id"], "org_id": org, "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
        assert visit.status_code in (200, 201), visit.text
    return {
        "org": org,
        "director": login(client, "p1140_dir", "passw0rd1"),
        "doctor": login(client, "p1140_doc", "passw0rd1"),
    }


def _screenings() -> int:
    from app.spd.models import SpdScreening

    with SessionLocal() as db:
        return db.query(SpdScreening).filter_by(program_code="hypertension").count()


def test_无机构的全域账号留空_422说清楚_不再恒扫0人(client, admin, world):
    before = _screenings()
    for who in (world["director"], admin):
        resp = client.post(f"{B}/screenings/auto-run", headers=who, json={"program_code": "hypertension"})
        assert resp.status_code == 422, resp.text   # 修前 200 {'scanned': 0, 'suspect': 0, …}
        assert resp.json() == {"detail": DETAIL}
    assert _screenings() == before


def test_全域账号填了机构照常扫(client, admin, world):
    for who in (world["director"], admin):
        resp = client.post(f"{B}/screenings/auto-run", headers=who,
                           json={"program_code": "hypertension", "org_id": world["org"]})
        assert resp.status_code == 200, resp.text
        assert {k: resp.json()[k] for k in ("scanned", "suspect")} == {"scanned": 3, "suspect": 3}


def test_挂了机构的账号留空照旧取本机构(client, world):
    resp = client.post(f"{B}/screenings/auto-run", headers=world["doctor"], json={"program_code": "hypertension"})
    assert resp.status_code == 200, resp.text
    assert {k: resp.json()[k] for k in ("scanned", "suspect")} == {"scanned": 3, "suspect": 3}
