"""监测趋势的 `latest` 在同一测定时刻几条读数时取后录的那条（P2-1603，第四十七批扫描 AK2-12）。

`measurement_trend` 按 `measured_at` 升序取窗口内全部读数、`latest` 取末行（`rows[-1]`），排序没有尾键：批量导入与设备
上传会造出同一时刻的多条，PG 上并列的几条谁排最后由执行计划定——趋势卡片的「最近一次」可以与监测清单排第一的（按
`measured_at, id` 倒序，P2-707）不是同一条。P2-707 的静态钉只扫 `.desc()` 的形状，看不到「升序取末行」。

修法：`order_by(SpdMeasurement.measured_at, SpdMeasurement.id)`；P2-707 的钉补上升序这一形
（`test_spd_latest_measurement_tiebreak.py::test_按测定时刻升序的也以编号收尾`）。SQLite 上并列时恰好按 rowid 返回，
行为上测不出修前修后之分：这里截下趋势这次请求实际发到库的 SQL，钉住 ORDER BY 以编号收尾，行为用例记下约定。
"""
from datetime import timedelta

from sqlalchemy import event

from app.clock import now_naive
from app.database import SessionLocal, engine
from app.spd.models import SpdMeasurement


def _patient(client, admin, id_card):
    resp = client.post("/api/patients", headers=admin, json={"name": "P21603 患者", "id_card": id_card})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _two_readings_same_moment(patient_id):
    at = (now_naive() - timedelta(days=1)).replace(microsecond=0)
    with SessionLocal() as db:
        db.add(SpdMeasurement(patient_id=patient_id, metric="bp_sys", value=168, unit="mmHg", measured_at=at))
        db.flush()
        db.add(SpdMeasurement(patient_id=patient_id, metric="bp_sys", value=128, unit="mmHg", measured_at=at))
        db.commit()
        return max(m.id for m in db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == patient_id))


def test_趋势查询发到库的排序以编号收尾(client, admin):
    patient_id = _patient(client, admin, "330102197001021603")
    _two_readings_same_moment(patient_id)
    statements: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        if "FROM spd_measurements" in statement and "ORDER BY" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    try:
        resp = client.get("/api/spd/measurements/trend", headers=admin,
                          params={"patient_id": patient_id, "metric": "bp_sys"})
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert resp.status_code == 200, resp.text
    orders = [" ".join(s.split("ORDER BY", 1)[1].split()) for s in statements]
    assert "spd_measurements.measured_at, spd_measurements.id" in orders, orders   # 修前只有 measured_at


def test_同一时刻两条读数_趋势的latest取后录的那条(client, admin):
    """行为上记下约定的次序（SQLite 上修前修后都绿，见模块文档）。"""
    patient_id = _patient(client, admin, "330102197001031603")
    newest = _two_readings_same_moment(patient_id)
    trend = client.get("/api/spd/measurements/trend", headers=admin,
                       params={"patient_id": patient_id, "metric": "bp_sys"}).json()
    assert (trend["latest"]["id"], trend["latest"]["value"]) == (newest, 128.0), trend["latest"]
    listed = client.get("/api/spd/measurements", headers=admin,
                        params={"patient_id": patient_id, "metric": "bp_sys"}).json()
    assert listed[0]["id"] == trend["latest"]["id"]   # 与监测清单排第一的是同一条
