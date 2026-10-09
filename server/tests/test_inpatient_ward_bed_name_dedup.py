"""建病区按比对键查重、建床位认得出数值相同的纯数字床号，撞上 409 点名已有写法（P2-1698，第五十批扫描 AN4-6 的「建」那一半）。

修前：`create_ward` 按字面查同机构病区名、`create_bed` 按字面查同病区床号；页面 `formJson` 不去空白。实测「外科病区 」（尾空格，
从别处复制来的）201，再建「外科病区」也 201；同病区建床「01」201，「1」也 201——两位患者分住「01」「1」都 201，「在院不可同床」
拦不住；病区和床位又都改不了名、撤不掉（P2-1357）。存量导入早有兄弟规矩（`scripts/import_legacy.py` 的床号孪生判据，P2-1125）：
同病区里数值相同、写法不同的纯数字床号不另建、点名已有写法。

修法：病区名按 `texttypes.text_key`（NFKC、去全部空白、不分大小写）在同机构里查重，撞上 409 点名已有写法；床号复用导入的同一个
判据——判据挪到 `routers/inpatient.py`（`bed_twin` / `bed_numbers_of`），导入脚本与建床接口都调它，导入行为不变（导入的原有
用例照旧绿）。字面完全相同的照旧 409 原文案。存量不动。HL7 A01 按字面找不到床的那一半另行登记，不在本条。
"""
import pytest


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"] for name in ("P21698 甲卫生院", "P21698 乙卫生院")]


def _ward(client, admin, org, name):
    return client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": name})


def _bed(client, admin, ward, bed_no):
    return client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": bed_no})


def _ward_names(client, admin, org):
    return [w["name"] for w in client.get(f"/api/inpatient/wards?org_id={org}", headers=admin).json()]


def test_带尾空格或全角空格的同名病区_409点名已有写法(client, admin, orgs):
    first = _ward(client, admin, orgs[0], "P21698 外科病区 ")
    assert first.status_code == 201, first.text
    for name in ("P21698 外科病区", "P21698　外科病区", "p21698 外科病区"):
        resp = _ward(client, admin, orgs[0], name)
        assert resp.status_code == 409, (name, resp.text)   # 修前 201，与「外科病区 」并存
        assert "「P21698 外科病区 」" in resp.json()["detail"], resp.text   # 点名已有的那种写法
    assert _ward_names(client, admin, orgs[0]) == ["P21698 外科病区 "]


def test_字面完全相同的照旧原文案(client, admin, orgs):
    resp = _ward(client, admin, orgs[0], "P21698 外科病区 ")
    assert (resp.status_code, resp.json()["detail"]) == (409, "该机构下病区已存在")


def test_别的机构同名病区照收(client, admin, orgs):
    resp = _ward(client, admin, orgs[1], "P21698 外科病区")
    assert resp.status_code == 201, resp.text


def test_同病区数值相同的纯数字床号_409点名已有写法(client, admin, orgs):
    ward = _ward(client, admin, orgs[0], "P21698 内科病区").json()["id"]
    assert _bed(client, admin, ward, "01").status_code == 201
    for bed_no in ("1", "001"):
        resp = _bed(client, admin, ward, bed_no)
        assert resp.status_code == 409, (bed_no, resp.text)   # 修前 201，「1」与「01」并存
        assert "「01」" in resp.json()["detail"] and f"「{bed_no}」" in resp.json()["detail"], resp.text
    again = _bed(client, admin, ward, "01")
    assert (again.status_code, again.json()["detail"]) == (409, "该病区下床号已存在")   # 字面相同照旧
    assert _bed(client, admin, ward, "A-01").status_code == 201   # 不是纯数字的不按数值认
    beds = client.get(f"/api/inpatient/beds?ward_id={ward}", headers=admin).json()
    assert sorted(b["bed_no"] for b in beds) == ["01", "A-01"]


def test_不同病区同床号照收(client, admin, orgs):
    ward = _ward(client, admin, orgs[0], "P21698 儿科病区").json()["id"]
    assert _bed(client, admin, ward, "1").status_code == 201


def test_导入脚本与建床接口用的是同一个判据():
    """判据只有一份：导入脚本不再自己写一份床号孪生判断（P2-1125 那一份挪到了路由里）。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import import_legacy

    from app.routers import inpatient

    assert import_legacy.bed_twin is inpatient.bed_twin
    assert import_legacy.bed_numbers_of is inpatient.bed_numbers_of
    assert not hasattr(import_legacy, "_bed_twin")
