"""积分商品下架、服务团队停用之后，管理端还列得出来、改得回去（P2-1580，第四十六批扫描 AJ4-4）。

商品清单 `GET /api/spd/goods` 原先写死只列上架的、没有参数，团队清单 `GET /api/spd/teams` 同样只列启用的；管理端的商品表
（专病考核与积分页）、「团队维护」表（服务团队页）就取这两个接口，编辑弹窗却有「上架 / 启用」选项。扫描实测（修前代码）：
保温杯缺货先下架后 `GET /goods` 返回 []，带 `?active=false`、`?include_inactive=true` 都被忽略、仍是 []；同编码重建 409
「该商品编码已存在」，补货后只能换编码另建；团队停用后同样从清单里消失。同一页的指标、方案、积分规则清单都连停用的一起列。

修法：照 P2-294（随访问卷）给两个清单加 `include_inactive`，缺省不变（村医端兑换页、各处团队下拉照旧只看上架 / 启用的）；
管理端两张表带上它、加状态列，下架 / 停用的经编辑弹窗改回。
"""
from pathlib import Path

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PAGE = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _between(start_marker: str, end_marker: str) -> str:
    start = PAGE.index(start_marker)
    return PAGE[start:PAGE.index(end_marker, start)]


def _goods(client, admin, **params):
    got = client.get(f"{B}/goods", headers=admin, params=params)
    assert got.status_code == 200, got.text
    return {g["code"]: g["active"] for g in got.json()}


def _teams(client, admin, org, **params):
    got = client.get(f"{B}/teams", headers=admin, params={"org_id": org, **params})
    assert got.status_code == 200, got.text
    assert got.headers["X-Total-Count"] == str(len(got.json()))   # 分页照旧，总数与筛过的对得上
    return {t["name"]: t["active"] for t in got.json()}


def test_下架的商品_带上include_inactive列得出来_上架后回到缺省清单(client, admin):
    made = client.post(f"{B}/goods", headers=admin, json={"code": "P21580_CUP", "name": "P21580 保温杯", "points": 50,
                                                          "stock": 0})
    assert made.status_code == 201, made.text
    url = f"{B}/goods/{made.json()['id']}"
    assert client.patch(url, headers=admin, json={"active": False}).status_code == 200   # 缺货先下架

    assert "P21580_CUP" not in _goods(client, admin)   # 缺省照旧只列上架的（村医端兑换页）
    assert _goods(client, admin, include_inactive=True)["P21580_CUP"] is False   # 修前列不出来
    again = client.post(f"{B}/goods", headers=admin, json={"code": "P21580_CUP", "name": "P21580 保温杯", "points": 50})
    assert again.status_code == 409   # 编码唯一照旧：要恢复就在原条目上改回上架

    restocked = client.patch(url, headers=admin, json={"active": True, "stock": 20, "stock_seen": 0})
    assert restocked.status_code == 200, restocked.text
    assert _goods(client, admin)["P21580_CUP"] is True


def test_停用的团队_带上include_inactive列得出来_启用后回到缺省清单(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21580 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for name in ("P21580 东镇团队", "P21580 西镇团队"):
        assert client.post(f"{B}/teams", headers=admin, json={"name": name, "org_id": org}).status_code == 201
    east = next(t["id"] for t in client.get(f"{B}/teams", headers=admin, params={"org_id": org}).json()
                if t["name"] == "P21580 东镇团队")
    assert client.patch(f"{B}/teams/{east}", headers=admin, json={"active": False}).status_code == 200   # 撤并

    assert _teams(client, admin, org) == {"P21580 西镇团队": True}   # 缺省照旧只列启用的
    assert _teams(client, admin, org, include_inactive=True) == {   # 修前东镇团队列不出来
        "P21580 东镇团队": False, "P21580 西镇团队": True}

    assert client.patch(f"{B}/teams/{east}", headers=admin, json={"active": True}).status_code == 200
    assert _teams(client, admin, org) == {"P21580 东镇团队": True, "P21580 西镇团队": True}


def test_管理端两张表连停用的一起取_带状态列_村医端兑换页照旧只取上架的():
    assert 'api("/api/spd/goods?include_inactive=true")' in _between("async function renderSpdAssess()", "$(\"#page-body\")")
    goods_table = _between('${table(["ID", "编码", "名称", "所需积分", "库存", "状态", "操作"], goods,', "编辑</button>")
    assert '<span class="tag">下架</span>' in goods_table

    assert 'api("/api/spd/teams?limit=100&include_inactive=true")' in _between(
        "async function renderSpdTeam()", "$(\"#page-body\")")
    team_table = _between('${table(["ID", "团队", "层级", "机构", "服务病种", "组长", "成员数", "状态", "操作"], teams,',
                          "编辑</button>")
    assert '<span class="tag">停用</span>' in team_table

    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    assert 'api("/api/spd/goods")' in doctor and "include_inactive" not in doctor
