"""机构分组建错了类型改不了：PATCH 带 group_type 回 200 却不改（P2-1511，第四十四批扫描 AH4-2 改类型那半）。

改分组的 `PATCH /api/org-groups/{id}` 只认名称、牵头机构、备注、启停四项，认不得的键被静默忽略、照样 200。新建表单的类型下拉缺省
第一项「片区/分片」，想建「胸痛专科联盟」忘了改下拉就落成 zone。修前实测（scan44 ah4/r2）：`PATCH {"group_type": "alliance"}`
200，类型仍是 zone；只送认不得的键、空体也都 200；没有删除分组的路由，只能停用——而按类型区别处理的 P2-487 / P2-1352 都以
「类型能更正」为前提。

修法：`GroupUpdate` 补 `group_type`，取值范围与建档的 `GroupIn` 同一个 pattern；送来的字段一项都没认上（认不得的键、不可空的
列传 null）时 422，照 P2-1368 的写法与措辞（「请至少改一项：…」）。可空的牵头机构传 null 是清空，算一项（P2-351）。分组能不能删
要业务拍板，不在这一条。
"""
import pytest

from app.database import SessionLocal
from app.models import OrgGroup

NOTHING_RECOGNIZED = "请至少改一项"


@pytest.fixture(scope="module")
def lead(client, admin):
    resp = client.post("/api/organizations", headers=admin, json={
        "name": "P21511 县人民医院", "org_type": "lead_hospital", "level": "county"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _group(client, admin, name: str, **extra) -> int:
    resp = client.post("/api/org-groups", headers=admin, json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _stored(gid: int) -> tuple:
    with SessionLocal() as db:
        group = db.get(OrgGroup, gid)
        return group.name, group.group_type, group.lead_org_id, group.note, group.active


def _coverage_groups(client, admin, group_type: str) -> int:
    return client.get(f"/api/org-groups/coverage?group_type={group_type}", headers=admin).json()["groups"]


def test_建成片区后改成专科联盟_读回专科联盟(client, admin, lead):
    gid = _group(client, admin, "P21511 胸痛专科联盟")   # 表单缺省类型：落成 zone
    assert _stored(gid)[1] == "zone"
    zones, alliances = _coverage_groups(client, admin, "zone"), _coverage_groups(client, admin, "alliance")
    resp = client.patch(f"/api/org-groups/{gid}", headers=admin, json={"group_type": "alliance"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["group_type"], resp.json()["group_type_name"]) == ("alliance", "专科联盟")   # 修前 200 而仍是 zone
    assert _stored(gid) == ("P21511 胸痛专科联盟", "alliance", None, "", True)
    listed = client.get("/api/org-groups?group_type=alliance", headers=admin).json()
    assert gid in [g["id"] for g in listed]
    # 覆盖情况按新类型计
    assert (_coverage_groups(client, admin, "zone"), _coverage_groups(client, admin, "alliance")) == (zones - 1, alliances + 1)


@pytest.mark.parametrize("n, body", list(enumerate([
    {"grouptype": "alliance"},   # 认不得的键：修前静默忽略、200
    {},
    {"name": None},
    {"group_type": None, "note": None, "active": None},   # 不可空的列传 null 照旧不改，忽略完一项都没有
])))
def test_一项都没认上的422_分组不动(client, admin, lead, n, body):
    gid = _group(client, admin, f"P21511 片区-{n}", lead_org_id=lead, note="原备注")
    before = _stored(gid)
    resp = client.patch(f"/api/org-groups/{gid}", headers=admin, json=body)
    assert resp.status_code == 422, (body, resp.text)
    assert NOTHING_RECOGNIZED in resp.json()["detail"], resp.text
    assert _stored(gid) == before


@pytest.mark.parametrize("bad", ["Zone", "union", ""])
def test_类型取值照建档_不在范围内的422(client, admin, lead, bad):
    gid = _group(client, admin, f"P21511 取值-{bad or 'blank'}")
    assert client.post("/api/org-groups", headers=admin, json={"name": f"P21511 建档-{bad or 'blank'}",
                                                               "group_type": bad}).status_code == 422
    resp = client.patch(f"/api/org-groups/{gid}", headers=admin, json={"group_type": bad})
    assert resp.status_code == 422, resp.text
    assert _stored(gid)[1] == "zone"


def test_原有能改的字段照旧200(client, admin, lead):
    gid = _group(client, admin, "P21511 原有字段")
    for body, expected in (
        ({"name": "P21511 原有字段改名"}, ("P21511 原有字段改名", "zone", None, "", True)),
        ({"note": "改备注"}, ("P21511 原有字段改名", "zone", None, "改备注", True)),
        ({"lead_org_id": lead}, ("P21511 原有字段改名", "zone", lead, "改备注", True)),
        ({"active": False}, ("P21511 原有字段改名", "zone", lead, "改备注", False)),
        ({"lead_org_id": None}, ("P21511 原有字段改名", "zone", None, "改备注", False)),   # 可空的传 null 即清空（P2-351），算一项
        ({"group_type": "grid", "unknown": 1}, ("P21511 原有字段改名", "grid", None, "改备注", False)),   # 认上了一项，认不得的照旧忽略
    ):
        resp = client.patch(f"/api/org-groups/{gid}", headers=admin, json=body)
        assert resp.status_code == 200, (body, resp.text)
        assert _stored(gid) == expected, body
