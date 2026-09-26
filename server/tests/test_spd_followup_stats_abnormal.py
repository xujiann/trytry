"""随访看板的「异常随访」卡片恒显示 0：页面读 `stats.abnormal`，接口只给 `by_abnormal`（P2-292）。

`renderSpdFollowup` 的卡片是 `["异常随访", stats.abnormal, stats.abnormal > 0]`，`GET /api/spd/followup-stats` 的出参里
却没有 `abnormal` 这个键——`spdCards` 把缺值渲染成 0，判出重度异常的随访再多，卡片上也是 0、从不标红。
医生移动端工作台的同名计数（`followups.abnormal`）一直有，口径是「判出中度 / 重度」，也就是会派处置任务的那两档。

修法：看板出参补 `abnormal`，与工作台走同一个判定（`service.followup_abnormal`）；派处置任务的门槛也改用同一组级别。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdFollowupRecord

    org, other = (client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"] for name in ("P2292 卫生院", "P2292 邻院"))
    created = client.post("/api/users", headers=admin, json={
        "username": "p2292_doc", "password": "pw123456", "full_name": "P2292 医生", "role": "doctor", "org_id": org})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2292 患者", "id_card": "330127197309092292"}).json()["id"]
    with SessionLocal() as db:
        for org_id, status, level in [
            (org, "done", "high"), (org, "done", "mid"), (org, "done", "low"), (org, "done", "none"),
            (org, "planned", "none"), (other, "done", "high"),
        ]:
            db.add(SpdFollowupRecord(patient_id=patient, org_id=org_id, planned_at="2026-09-20", status=status,
                                     abnormal_level=level))
        db.commit()
    return {"org": org, "doctor": _login(client, "p2292_doc")}   # 主任是全域角色，看本机构得用医生


def test_看板给出异常随访数_只数中度重度_只数本机构的(client, world):
    stats = client.get(f"{B}/followup-stats", headers=world["doctor"])
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["abnormal"] == 2   # 修前没有这个键，卡片恒 0
    assert body["by_abnormal"] == {"high": 1, "low": 1, "mid": 1, "none": 1}   # 分布照旧：只数已完成的


def test_看板与工作台同一个口径(client, admin, world):
    board = client.get(f"{B}/followup-stats", headers=world["doctor"]).json()
    region = client.get(f"{B}/stats/region", params={"org_id": world["org"]}, headers=admin)
    assert region.status_code == 200, region.text
    assert region.json()["followups"]["abnormal"] == board["abnormal"] == 2
