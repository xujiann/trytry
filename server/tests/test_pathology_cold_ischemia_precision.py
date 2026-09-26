"""冷缺血时间：只填日期的按零点算、录反了的悄悄记成「未采集」（P2-270）。

冷缺血时间＝固定 − 离体，按分钟判「超 60 分钟」。时间形状（`OptionalDateTimeSecStr`）收只填日期的写法：离体只填
「2026-08-12」、固定填到「2026-08-12 09:20」，原先按零点算出 560 分钟、记成超 60 分钟；固定早于离体（录反了）登记照收，
算出负数就当成「未采集」——统计上和没填一样，谁也不知道是录错了。修法：登记时两个时间都须填到分钟、固定不得早于离体
（422 说清楚）；存量里只填了日期的不再按零点算（算没填全）。
"""
import pytest


@pytest.fixture(scope="module")
def request_id(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2270 病理医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2270 患者", "id_card": "330881199202027270"}).json()["id"]
    return client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": org, "center_type": "pathology",
        "item_code": "P2270", "item_name": "组织病理学检查"}).json()["id"]


def _submit(client, admin, request_id, **times):
    return client.post("/api/pathology/specimens", headers=admin, json={"request_id": request_id, **times})


@pytest.mark.parametrize("times, needle", [
    ({"excised_at": "2026-08-12", "fixed_at": "2026-08-12 09:20"}, "离体时间须填到分钟"),
    ({"excised_at": "2026-08-12 09:00", "fixed_at": "2026-08-12"}, "固定时间须填到分钟"),
    ({"excised_at": "2026-08-12T11:00:00", "fixed_at": "2026-08-12T10:00:00"}, "固定时间早于离体时间"),
], ids=["离体只填日期", "固定只填日期", "录反了"])
def test_登记时说清楚_不再悄悄算错或记成未采集(client, admin, request_id, times, needle):
    resp = _submit(client, admin, request_id, **times)
    assert resp.status_code == 422, resp.text   # 修前 201：算出 560 分钟 / 记成「未采集」
    assert needle in resp.text


def test_填到分钟的照收_只填一个的照旧算未采集(client, admin, request_id):
    ok = _submit(client, admin, request_id, excised_at="2026-08-12 09:00", fixed_at="2026-08-12 09:20")
    assert ok.status_code == 201 and ok.json()["cold_ischemia_minutes"] == 20, ok.text
    partial = _submit(client, admin, request_id, excised_at="2026-08-12 09:00")
    assert partial.status_code == 201 and partial.json()["cold_ischemia_minutes"] is None


def test_存量里只填了日期的不按零点算(client, admin, request_id):
    from app.database import SessionLocal
    from app.models import PathologySpecimen
    from app.routers.pathology import _out

    with SessionLocal() as db:
        legacy = PathologySpecimen(request_id=request_id, specimen_no="PS-P2270-LEGACY",
                                   excised_at="2026-08-12", fixed_at="2026-08-12 09:20")
        db.add(legacy)
        db.commit()
        assert _out(legacy)["cold_ischemia_minutes"] is None   # 修前 560：按零点算
