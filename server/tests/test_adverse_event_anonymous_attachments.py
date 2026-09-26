"""匿名上报的不良事件，附件清单把上报人带了出来（P2-401）。

「匿名上报不落报告人（匿名可选：鼓励上报）」：事件本身不存报告人，页面写着「不良事件上报（可匿名）」。可同一页上传的
佐证照片记着 `uploaded_by`，`GET /api/attachments?owner_type=adverse_event` 把它发给同院任一账号与管理层——编号在别的
清单上对得上姓名，匿名形同虚设。修后匿名事件的附件清单上传人一律 null（键在，与居民端上传同一种空值），附件行与审计
照旧留着上传人；实名事件照旧。
"""
import io

import pytest

from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2401 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = {}
    for username, role in (("p2401_nurse", "operator"), ("p2401_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": username})
        assert created.status_code == 201, created.text
        heads[username] = login(client, username, "passw0rd1")
        heads[username + "_id"] = created.json()["id"]
    return {"org": org, **heads}


def _event_with_photo(client, world, anonymous):
    event = client.post("/api/quality/adverse-events", headers=world["p2401_nurse"], json={
        "org_id": world["org"], "event_type": "fall", "level": "III", "description": "P2401 病区跌倒",
        "anonymous": anonymous})
    assert event.status_code == 201, event.text
    event_id = event.json()["id"]
    uploaded = client.post("/api/attachments", headers=world["p2401_nurse"],
                           data={"owner_type": "adverse_event", "owner_id": str(event_id)},
                           files={"file": ("scene.pdf", io.BytesIO(b"%PDF-1.4 P2401 scene"), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    return event_id


def _uploaders(client, world, event_id):
    rows = client.get("/api/attachments", headers=world["p2401_dir"],
                      params={"owner_type": "adverse_event", "owner_id": event_id}).json()
    return [r["uploaded_by"] for r in rows]


def test_匿名事件的附件清单不出上传人(client, world):
    event_id = _event_with_photo(client, world, anonymous=True)
    assert _uploaders(client, world, event_id) == [None]   # 修前：上报护士的账号编号


def test_实名事件照旧出上传人(client, world):
    event_id = _event_with_photo(client, world, anonymous=False)
    assert _uploaders(client, world, event_id) == [world["p2401_nurse_id"]]
