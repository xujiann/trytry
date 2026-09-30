"""DRG 分组勾了「必须命中主手术」却没填主手术关键词、或两类关键词都空：照建 201，之后永不入组（P2-1019，第二十九批「字段之间的
约束」扫描 E3-10）。

`_match_score`：`require_procedure` 且主手术关键词一个有效词都没有，恒不命中；两类都没有有效词，同样恒不命中。原先建组、改组都不查，
「甲状腺结节 + 甲状腺部分切除术」预判不命中，出院入组落到 QY 兜底组，建组的人看不出来。与 P2-466 / P2-712「永不命中、也不报错
的配置在建的时候拦」同一条规矩。修法：新建时判；改档只在动了匹配配置时与存量合并后判，只改名 / 权重 / 停用的照旧放行。
"""
import pytest

G = "/api/drgs/groups"


def _group(code, **kw):
    return {"code": code, "name": f"{code} 组", "base_weight": 1.2, **kw}


@pytest.mark.parametrize("body", [
    _group("P1019A", keywords="甲状腺结节", require_procedure=True),
    _group("P1019B", keywords="甲状腺结节", procedure_keywords=" ，、", require_procedure=True),   # 只有分隔符与空白
    _group("P1019C"),
    _group("P1019D", keywords="，", procedure_keywords=" "),
])
def test_永远入不了组的配置建不进去(client, admin, body):
    got = client.post(G, headers=admin, json=body)
    assert got.status_code == 422, got.text   # 修前 201


def test_正常的组照建_改档动了匹配配置才判_只改名放行(client, admin):
    ok = client.post(G, headers=admin, json=_group("P1019OK", keywords="甲状腺结节",
                                                  procedure_keywords="甲状腺部分切除术", require_procedure=True))
    assert ok.status_code == 201, ok.text
    gid = ok.json()["id"]
    bad = client.patch(f"{G}/{gid}", headers=admin, json={"procedure_keywords": ""})
    assert bad.status_code == 422, bad.text   # 与存量合并后：仍勾着必须命中主手术，手术词却清空了
    assert client.patch(f"{G}/{gid}", headers=admin, json={"require_procedure": False, "procedure_keywords": ""}).status_code == 200
    assert client.patch(f"{G}/{gid}", headers=admin, json={"name": "改个名"}).status_code == 200


def test_存量写坏的组改名停用不挡(client, admin):
    from app.database import SessionLocal
    from app.models import DrgGroup

    with SessionLocal() as db:
        broken = DrgGroup(code="P1019OLD", name="存量写坏", base_weight=1.0, keywords="", procedure_keywords="",
                          require_procedure=True)
        db.add(broken)
        db.commit()
        gid = broken.id
    assert client.patch(f"{G}/{gid}", headers=admin, json={"name": "存量写坏（待修）"}).status_code == 200
    assert client.patch(f"{G}/{gid}", headers=admin, json={"active": False}).status_code == 200
    assert client.patch(f"{G}/{gid}", headers=admin, json={"keywords": "甲状腺结节"}).status_code == 422   # 手术词仍空
