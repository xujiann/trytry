"""积分商品、慢专病团队的「编辑」把状态固定预填成「上架 / 启用」并且恒送：别人刚下架或停用的，旧页面改个积分、改个名
就又回来了（P2-968，第二十七批「丢失更新：编辑时把页面载入的整行值写回」扫描 G1-7）。

商品编辑框的状态栏固定 `value: "1"`（上架）、提交恒送 `active`；团队编辑框同样固定「启用」，名称、层级、状态恒送。两张清单
只列启用的，所以这个常量就是载入时的状态——甲把保温杯下架（停止供货），乙在旧页面上把所需积分从 1 改成 2，商品重新上架，
村医又能兑换，积分扣了却发不出货；停用（撤并）的团队被旧页面改个名，也重新启用、可以再分发患者。P2-920 只把库存那一格
改成了「只送改过的」。

修法：两个编辑框的状态按行上的现值预填、只在改了才送；团队的名称与层级同样只在改了才送。后端本就「不传即不改」。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
B = "/api/spd"


def _between(start_marker: str, end_marker: str) -> str:
    start = PAGE.index(start_marker)
    return PAGE[start:PAGE.index(end_marker, start)]


def test_商品编辑_状态按现值预填_只在改了才送():
    assert 'data-active="${g.active ? "1" : "0"}"' in _between('data-goods-edit="${g.id}"', "编辑</button>")
    dialog = _between("if (goodsEdit) {", "/api/spd/goods/${d.goodsEdit}")
    status = dialog[dialog.index('name: "active"'):]
    assert 'value: goodsEdit.dataset.active' in status[:status.index("options")]   # 修前固定 "1"（上架）
    assert "const body = {};" in dialog   # 修前 { active: … } 恒送
    assert "if (form.active !== (d.active" in dialog


def test_团队编辑_名称层级状态都只在改了才送():
    assert 'data-active="${t.active === false ? "0" : "1"}"' in _between('data-team-edit="${t.id}"', "编辑</button>")
    dialog = _between("if (teamEdit) {", "/api/spd/teams/${d.teamEdit}")
    status = dialog[dialog.index('name: "active"'):]
    assert 'value: d.active' in status[:status.index("options")]   # 修前固定 "1"（启用）
    assert "const body = {};" in dialog   # 修前 { name, level, active } 恒送
    for field in ("name", "level"):
        assert f"if (form.{field} !== d.{field}) body.{field} = form.{field};" in dialog
    assert "if (form.active !== (d.active" in dialog


def test_旧页面只送改过的_下架的商品与停用的团队不回来(client, admin):
    goods = client.post(f"{B}/goods", headers=admin, json={"code": "P2968-CUP", "name": "P2968 保温杯", "points": 1,
                                                           "stock": 5})
    assert goods.status_code == 201, goods.text
    goods_id = goods.json()["id"]
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2968 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    team = client.post(f"{B}/teams", headers=admin, json={"name": "P2968 甲团队", "org_id": org})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    # 甲：下架、停用
    assert client.patch(f"{B}/goods/{goods_id}", headers=admin, json={"active": False}).status_code == 200
    assert client.patch(f"{B}/teams/{team_id}", headers=admin, json={"active": False}).status_code == 200
    # 乙在旧页面上只改积分、只改名：修后页面只送改过的那一格
    assert client.patch(f"{B}/goods/{goods_id}", headers=admin, json={"points": 2}).status_code == 200
    assert client.patch(f"{B}/teams/{team_id}", headers=admin, json={"name": "P2968 甲团队（更名）"}).status_code == 200
    assert goods_id not in {g["id"] for g in client.get(f"{B}/goods", headers=admin).json()}
    assert team_id not in {t["id"] for t in client.get(f"{B}/teams", headers=admin, params={"org_id": org}).json()}
