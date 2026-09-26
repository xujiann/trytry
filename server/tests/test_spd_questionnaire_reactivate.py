"""随访问卷停用之后界面上启用不回来：问卷清单只列启用的，管理表拿不到停用的那张（P2-294）。

随访页「随访问卷与异常分级」表上有「状态」列（停用标灰）、编辑弹窗里有「启用 / 停用」，而它取数的
`GET /api/spd/questionnaires` 只列启用的——停用的问卷当场从表里消失，「停用」标签与「启用」选项永远用不上，
停了就只能调接口启回来。同文件的随访方案清单一直是连停用的一起列、带状态标签。

修法：清单加 `include_inactive`（缺省不变，选问卷的下拉照旧只看启用的）；随访页的管理表带上它，
新建方案的问卷下拉在前端只列启用的。
"""
from pathlib import Path

B = "/api/spd"
PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js"


def _codes(client, admin, **params):
    got = client.get(f"{B}/questionnaires", params=params, headers=admin)
    assert got.status_code == 200, got.text
    return {q["code"]: q["active"] for q in got.json()}


def test_停用的问卷_带上include_inactive列得出来_启用后回到缺省清单(client, admin):
    created = client.post(f"{B}/questionnaires", headers=admin, json={"code": "P2294_Q", "name": "P2294 问卷"})
    assert created.status_code == 201, created.text
    url = f"{B}/questionnaires/{created.json()['id']}"
    assert client.patch(url, headers=admin, json={"active": False}).status_code == 200

    assert "P2294_Q" not in _codes(client, admin)   # 缺省照旧只列启用的
    assert _codes(client, admin, include_inactive=True)["P2294_Q"] is False   # 修前列不出来

    assert client.patch(url, headers=admin, json={"active": True}).status_code == 200
    assert _codes(client, admin)["P2294_Q"] is True


def test_随访页管理表连停用的一起取_新建方案的下拉只列启用的():
    page = PAGE.read_text(encoding="utf-8")
    assert 'api("/api/spd/questionnaires?include_inactive=true")' in page
    assert 'questionnaires.filter((q) => q.active !== false).map((q) =>' in page
