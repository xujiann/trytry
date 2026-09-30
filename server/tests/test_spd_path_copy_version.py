"""慢专病路径「复制」按源版本号 +1 派生新版本：复制过一次后，再从旧版复制一律 409，界面没有填版本号的入口（P2-992，
第二十八批「编号、单号与流水号」扫描 F3-8）。

`copy_path_template` 不带版本号时原先取 `_bump_version(源版本)`：v1 复制出 v2 之后，再从 v1 复制推出的还是 v2、从 v2 复制推出
v3——只要 v3 已被别的复制占了，就撞「编码 + 版本」唯一约束 409「目标编码与版本已存在」；页面上的「复制」按钮只送 `{}`。

修法：不指定版本号时取目标编码下的下一个空号——v 加数字的按同编码已有的最大号 + 1（与证明编号 P2-897 同形），自定义版本号
追加的 -r2 已占用就依次试 -r3、-r4。指定了版本号的照旧按指定的建、撞了照旧 409。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def program_id(client, admin):
    return client.get(f"{B}/programs", headers=admin).json()[0]["id"]


def _template(client, admin, program_id, code, version):
    made = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program_id, "code": code, "name": f"{code} 路径", "version": version})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _copy(client, admin, template_id, **body):
    return client.post(f"{B}/path-templates/{template_id}/copy", headers=admin, json=body)


def test_从旧版再复制_取同编码的下一个空号(client, admin, program_id):
    v1 = _template(client, admin, program_id, "P2992-A", "v1")
    first = _copy(client, admin, v1)
    assert first.status_code == 201 and first.json()["version"] == "v2", first.text
    again = _copy(client, admin, v1)
    assert again.status_code == 201 and again.json()["version"] == "v3", again.text   # 修前 409
    from_v2 = _copy(client, admin, first.json()["id"])
    assert from_v2.status_code == 201 and from_v2.json()["version"] == "v4", from_v2.text   # 修前 409（v3 已占）


def test_自定义版本号_依次追加r2_r3(client, admin, program_id):
    custom = _template(client, admin, program_id, "P2992-B", "2026A")
    assert _copy(client, admin, custom).json()["version"] == "2026A-r2"
    assert _copy(client, admin, custom).json()["version"] == "2026A-r3"   # 修前 409


def test_指定了版本号的照旧_撞了409(client, admin, program_id):
    v1 = _template(client, admin, program_id, "P2992-C", "v1")
    assert _copy(client, admin, v1, version="v9").json()["version"] == "v9"
    clash = _copy(client, admin, v1, version="v9")
    assert clash.status_code == 409, clash.text
