"""冷缺血「超 60 分钟」按秒判：离体到固定 60 分 50 秒原先不算超时（P2-133）。

质控统计先把冷缺血时间截成整分钟、再比 `> 60`：60 分 50 秒截成 60，不计入 `over_60min`；均值同样按截断后的整分钟算，
偏低。时间戳本就收到秒（P1-100 起「秒可有可无」），超没超 60 分钟应当按实际时长判。

修法：判超时与算均值按秒；逐条回显的 `cold_ischemia_minutes` 照旧是整分钟（向下取整），响应形状不变。
"""
import itertools

_SEQ = itertools.count(1)


def _specimen(client, admin, excised, fixed):
    n = next(_SEQ)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2133 患者{n}", "id_card": f"33010619700909{n:04d}", "gender": "男"})
    assert patient.status_code == 201, patient.text
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P2133 医院{n}", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": patient.json()["id"], "from_org_id": org, "center_type": "pathology",
        "item_code": "P001", "item_name": "组织病理学检查"})
    assert req.status_code == 201, req.text
    resp = client.post("/api/pathology/specimens", headers=admin, json={
        "request_id": req.json()["id"], "site": "胃窦", "excised_at": excised, "fixed_at": fixed})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cold(client, admin):
    return client.get("/api/pathology/specimen-stats", headers=admin).json()["cold_ischemia"]


def test_六十分五十秒算超时_整六十分钟不算(client, admin):
    before = _cold(client, admin)
    over = _specimen(client, admin, "2026-09-24 08:00:00", "2026-09-24 09:00:50")
    exact = _specimen(client, admin, "2026-09-25 08:00:00", "2026-09-25 09:00:00")
    assert (over["cold_ischemia_minutes"], exact["cold_ischemia_minutes"]) == (60, 60)   # 逐条回显照旧是整分钟
    after = _cold(client, admin)
    assert after["measured"] == before["measured"] + 2
    assert after["over_60min"] == before["over_60min"] + 1   # 修前 +0：60 分 50 秒截成 60，不算超时


def test_均值按秒算(client, admin):
    """只有这一条时均值就是它本身：45 分 30 秒记 45.5（修前按截断的 45）。"""
    from app.database import SessionLocal
    from app.models import PathologySpecimen

    with SessionLocal() as db:   # 共用库里别的用例的标本会进均值：先挪开，用完原样放回
        saved = [(s.id, s.excised_at, s.fixed_at) for s in db.query(PathologySpecimen).all()]
        for s in db.query(PathologySpecimen).all():
            s.excised_at, s.fixed_at = "", ""
        db.commit()
    try:
        _specimen(client, admin, "2026-09-26 08:00:00", "2026-09-26 08:45:30")
        assert _cold(client, admin)["avg_minutes"] == 45.5
    finally:
        with SessionLocal() as db:
            for sid, excised, fixed in saved:
                row = db.get(PathologySpecimen, sid)
                row.excised_at, row.fixed_at = excised, fixed
            db.commit()
