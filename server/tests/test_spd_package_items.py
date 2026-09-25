"""服务包的项目次数写成文字：建服务包 500；改服务包照收，之后每次绑定这个服务包都 500（P2-82）。

绑定服务包时按 `int(times)` 把每个项目折成可用次数。建服务包的校验这一句自己就 `int(times)`——次数写成文字当场
`ValueError`、500；改服务包干脆不查项目，文字次数存得进去，之后绑定一次 500 一次。界面的新建表单把次数转成数、
编辑弹窗不改项目，写得进去的是接口调用方。

修法：建 / 改服务包同一句（`service.package_items_ok`，报错文案沿用原文），读得成整数的照旧收（"3"、2.5）；
修前改出来的坏项目，绑定时 422 说清楚、不 500。
"""
import pytest

B = "/api/spd"
MESSAGE = "服务包项目须有编码且次数大于0"


def _package(client, admin, code, items):
    return client.post(f"{B}/service-packages", headers=admin, json={"code": code, "name": f"{code} 包", "items": items})


@pytest.mark.parametrize("items", [
    [{"code": "BP", "name": "测血压", "times": "三"}],
    [{"code": "BP", "name": "测血压", "times": None}],
    [{"name": "没编码", "times": 3}],
    [{"code": "BP", "times": 0}],
], ids=["次数是文字", "次数是null", "没有编码", "次数为0"])
def test_建服务包_项目不对_422原文(client, admin, items):
    """修前前两种 500（校验这一句自己抛错），后两种本就 422。"""
    resp = _package(client, admin, "P282_BAD", items)
    assert resp.status_code == 422 and resp.json() == {"detail": MESSAGE}, resp.text[:300]


def test_读得成整数的次数照旧收(client, admin):
    resp = _package(client, admin, "P282_STR", [{"code": "BP", "times": "3"}, {"code": "GLU", "times": 2.5}])
    assert resp.status_code == 201, resp.text


def test_改服务包也查项目(client, admin):
    created = _package(client, admin, "P282_EDIT", [{"code": "BP", "name": "测血压", "times": 4}])
    assert created.status_code == 201, created.text
    url = f"{B}/service-packages/{created.json()['id']}"
    for items in ([{"code": "BP", "times": "四"}], [{"name": "没编码", "times": 1}]):
        resp = client.patch(url, headers=admin, json={"items": items})   # 修前 200
        assert resp.status_code == 422 and resp.json() == {"detail": MESSAGE}, resp.text[:300]
    resp = client.patch(url, headers=admin, json={"name": "P282 改名"})   # 不动项目的改档不查
    assert resp.status_code == 200, resp.text


def test_存量坏项目的服务包_绑定422说清楚_不500(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdServicePackage

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P282 服务包院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P282 患者", "id_card": "330106197001012015", "gender": "女", "birth_date": "1970-01-01"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin,
                             json={"patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    with SessionLocal() as db:
        legacy = SpdServicePackage(code="P282_LEGACY", name="P282 存量坏包", active=True,
                                   items=[{"code": "BP", "name": "测血压", "times": "三"}])
        db.add(legacy)
        db.commit()
        package_id = legacy.id
    resp = client.post(f"{B}/enrollments/{enrollment.json()['id']}/packages", headers=admin,
                       json={"package_id": package_id})
    assert resp.status_code == 422, resp.text[:300]   # 修前 int("三") 抛错、500
    assert resp.json() == {"detail": "服务包的项目配置有误（须有编码且次数大于0），暂不能绑定，请先修正服务包"}
