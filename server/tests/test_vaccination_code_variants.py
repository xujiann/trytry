"""接种禁忌与既往剂次认得出同一个疫苗编码的不同写法（P1-219，第二十一批「同一个值的不同写法」扫描 N3-2；与审方 P1-218 同一个
`texttypes.code_key`）。

禁忌登记是手输框，接种登记选批次后自动带出批次上的编码，原先两边按原样比：禁忌登记成 `hepb` / `'HepB '`、批次是 `HepB` 的，
接种照样 201；接种前评估按 `hepb` 查报「可以接种、既往 0 剂」，实际已按 `HepB` 打过 1 剂。修法：禁忌的硬拦截、接种前评估的
禁忌史与既往剂次都按比对键认；落库的编码不动（疫苗编码规范另行登记）。
"""
import pytest

B = "/api/vaccination"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1219 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    batch = client.post("/api/vaccine-supply/batches", headers=admin, json={
        "vaccine_code": "P1219HepB", "vaccine_name": "乙肝疫苗", "batch_no": "P1219-B001", "expire_date": "2099-12-31",
        "org_id": org, "quantity": 20})
    assert batch.status_code == 201, batch.text
    return {"org": org, "batch": batch.json()["id"], "n": 0}


def _kid(client, admin, world):
    world["n"] += 1
    kid = client.post("/api/patients", headers=admin, json={
        "name": f"P1219 受种者{world['n']}", "id_card": f"11010120230101{1218 + world['n']:04d}",
        "birth_date": "2023-01-01"}).json()["id"]
    return kid


def _contra(client, admin, kid, code):
    resp = client.post(f"{B}/contraindications", headers=admin, json={
        "patient_id": kid, "vaccine_code": code, "reason": f"乙肝疫苗成分过敏（{code!r}）", "contra_type": "permanent"})
    assert resp.status_code == 201, resp.text


def _vaccinate(client, admin, world, kid, code="P1219HepB"):
    return client.post(f"{B}/records", headers=admin, json={
        "patient_id": kid, "vaccine_code": code, "vaccine_name": "乙肝疫苗", "dose_no": 1, "org_id": world["org"],
        "batch_id": world["batch"]})


@pytest.mark.parametrize("code", ["p1219hepb", "P1219HepB ", "Ｐ１２１９ＨｅｐＢ"], ids=["小写", "尾随空格", "全角"])
def test_禁忌写法与批次编码不同_照样拦(client, admin, world, code):
    kid = _kid(client, admin, world)
    _contra(client, admin, kid, code)
    pre = client.get(f"{B}/pre-check", headers=admin, params={"patient_id": kid, "vaccine_code": "P1219HepB"}).json()
    assert pre["allowed"] is False and pre["contraindications"], pre   # 修前 True []
    resp = _vaccinate(client, admin, world, kid)
    assert resp.status_code == 409 and "存在接种禁忌" in resp.json()["detail"], resp.text   # 修前 201


def test_接种前评估按另一种写法查_既往剂次照样算上(client, admin, world):
    kid = _kid(client, admin, world)
    assert _vaccinate(client, admin, world, kid).status_code == 201
    for code in ("p1219hepb", "P1219HepB"):
        pre = client.get(f"{B}/pre-check", headers=admin, params={"patient_id": kid, "vaccine_code": code}).json()
        assert (pre["previous_doses"], pre["next_dose_no"]) == (1, 2), (code, pre)   # 修前按小写查 0 剂、第 1 剂


def test_别的疫苗的禁忌照旧不拦(client, admin, world):
    kid = _kid(client, admin, world)
    _contra(client, admin, kid, "P1219BCG")
    assert _vaccinate(client, admin, world, kid).status_code == 201
