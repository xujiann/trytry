"""宣教素材停用之后，管理端还列得出来、改得回启用（P2-1643，第四十八批扫描 AL2-6）。

素材清单 `GET /api/spd/edu-materials` 原先写死只列启用的、没有参数；运行中枢页的宣教素材表就取这个接口，表里有「状态」
列、编辑弹窗有「启用 / 停用」，却永远碰不到停用的那几行。扫描实测（修前代码）：停用 200 → 缺省清单里没有它，
带 `include_inactive=true` 也被忽略、仍没有 → 同编码重建 409「该宣教编码已存在」——停用一次就再也启用不回来。

修法：照 P2-1580（商品、团队）与 P2-294（随访问卷）给清单加 `include_inactive`，缺省不变（成员端宣教推送选素材的下拉照旧
只列启用的）；管理端素材表带上它，状态列显示停用。素材是全县共用的配置、清单本不分机构，分页照旧。
"""
from pathlib import Path

from jssrc import strip_comments

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _renderer(name: str) -> str:
    source = strip_comments((STATIC / "pages-spd.js").read_text(encoding="utf-8"))
    start = source.index(f"async function {name}(")
    return source[start:source.index("\nasync function ", start + 1)]


def _materials(client, admin, **params):
    got = client.get(f"{B}/edu-materials", headers=admin, params={"keyword": "P21643", **params})
    assert got.status_code == 200, got.text
    assert got.headers["X-Total-Count"] == str(len(got.json()))   # 分页照旧，总数与筛过的对得上
    return {m["code"]: m["active"] for m in got.json()}


def test_停用的素材_带上include_inactive列得出来_启用后回到缺省清单(client, admin):
    made = {}
    for code, title in (("P21643_SALT", "P21643 低盐饮食"), ("P21643_WALK", "P21643 饭后散步")):
        created = client.post(f"{B}/edu-materials", headers=admin, json={"code": code, "title": title})
        assert created.status_code == 201, created.text
        made[code] = created.json()["id"]
    url = f"{B}/edu-materials/{made['P21643_SALT']}"
    stopped = client.patch(url, headers=admin, json={"active": False})
    assert stopped.status_code == 200, stopped.text

    assert _materials(client, admin) == {"P21643_WALK": True}   # 缺省照旧只列启用的（成员端推送选素材）
    assert _materials(client, admin, include_inactive=True) == {   # 修前被忽略、列不出停用的那条
        "P21643_SALT": False, "P21643_WALK": True}
    again = client.post(f"{B}/edu-materials", headers=admin, json={"code": "P21643_SALT", "title": "P21643 低盐饮食"})
    assert again.status_code == 409   # 编码唯一照旧：要恢复就在原条目上改回启用

    restored = client.patch(url, headers=admin, json={"active": True})
    assert restored.status_code == 200, restored.text
    assert _materials(client, admin) == {"P21643_SALT": True, "P21643_WALK": True}


def test_管理端素材表连停用的一起取_带状态列_成员端推送照旧只取启用的():
    admin_page = _renderer("renderSpdAdmin")
    assert 'api("/api/spd/edu-materials?limit=100&include_inactive=true")' in admin_page   # 修前不带这个参数
    table_start = admin_page.index('${table(["ID", "编码", "标题", "病种", "形式", "科室", "状态", "操作"], materials,')
    material_table = admin_page[table_start:admin_page.index("编辑</button>", table_start)]
    assert '<span class="tag">停用</span>' in material_table and "data-active=" in material_table

    member_page = _renderer("renderSpdMember")
    assert 'api("/api/spd/edu-materials?limit=100")' in member_page and "include_inactive" not in member_page
