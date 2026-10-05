"""室内质控失控点的处理时刻随清单与处理回执返回，页面「已处理」一格写出来（P2-1472，第四十三批扫描 AG1-12）。

修前处理接口写着「处理人与时刻留痕」，`handled_at` 也落了库，`MeasurementOut` 却没有这一键：清单、处理回执、页面都看不到
何时处理的，质控检查核不了「处理在发报告之前还是之后」（P2-492 补齐了原因、纠正措施、处理人，唯独漏了时刻）。

修法：`MeasurementOut` 末尾只增 `handled_at`，未处理为 null；写法照全站出参惯例——落库时刻（naive UTC）的 isoformat，与别的
接口的时间戳同一写法、能直接比，不在出参里按服务器时区换成本地（显示用哪个时区是待裁定的 P1-105；修复子代理原先换成本地
墙上时间，核实时改回惯例）。录入回执继承它（新录的点恒为 null，排在原有尾键 `unhandled_before` / `alert` 之前）。页面
「已处理」一格在处理人后面写出处理时刻，照全站惯例截到分钟。这里把进程时区拨到东八区：出参照样是库里的 UTC 值。
"""
import os
import re
import shutil
import time

import pytest

from labqc_page import responses, run

from app.database import SessionLocal
from app.models import QcMeasurement

OLD_KEYS = ["id", "lot_id", "value", "measured_at", "operator", "warning", "out_of_control", "violated_rules",
            "handled", "handle_reason", "corrective_action", "handled_by"]
HANDLE = {"reason": "质控品复溶后放置过久失效", "corrective_action": "更换新支质控品、重新定标后复测"}


@pytest.fixture
def shanghai():
    """把进程时区拨到东八区，用完拨回去（同 test_labqc_measured_at_local.py）。"""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


@pytest.fixture(scope="module")
def lot(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21472 县检验中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    made = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "K", "item_name": "血清钾", "lot_no": "P21472", "target_value": 5.0, "sd": 0.5})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _rows(client, admin, lot_id):
    resp = client.get(f"/api/labqc/lots/{lot_id}/measurements", headers=admin)
    assert resp.status_code == 200, resp.text
    return {r["id"]: r for r in resp.json()}


def test_清单与处理回执都带处理时刻_处理前为null(client, admin, lot, shanghai):
    created = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin, json={"value": 7.0})   # z=+4 1-3s
    assert created.status_code == 201 and created.json()["out_of_control"], created.text
    point = created.json()
    assert list(point) == OLD_KEYS + ["handled_at", "unhandled_before", "alert"] and point["handled_at"] is None

    before = _rows(client, admin, lot)[point["id"]]
    assert list(before) == OLD_KEYS + ["handled_at"]   # 修前没有这一键
    assert before["handled_at"] is None

    handled = client.post(f"/api/labqc/measurements/{point['id']}/handle", headers=admin, json=HANDLE)
    assert handled.status_code == 200, handled.text
    receipt = handled.json()
    assert list(receipt) == OLD_KEYS + ["handled_at"]
    with SessionLocal() as db:
        stored = db.get(QcMeasurement, point["id"]).handled_at   # 库里照旧是 naive UTC
    # 全站惯例：落库时刻的 isoformat，进程时区拨到东八区也不换算
    assert receipt["handled_at"] == stored.isoformat()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?", receipt["handled_at"]), receipt["handled_at"]

    after = _rows(client, admin, lot)[point["id"]]
    assert (after["handled"], after["handled_by"], after["handled_at"]) == (True, receipt["handled_by"], receipt["handled_at"])


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_页面已处理一格写出处理时刻(client, admin, lot):
    point = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin, json={"value": 2.8}).json()   # z=-4.4
    assert point["out_of_control"], point
    handled = client.post(f"/api/labqc/measurements/{point['id']}/handle", headers=admin, json=HANDLE)
    assert handled.status_code == 200, handled.text
    row = _rows(client, admin, lot)[point["id"]]
    assert row["handled_at"], row
    detail = run(responses(client, admin, [lot]),
                 "await renderLabQc(); await openLot(ARGS.params.lot); return document.querySelector('#lot-detail').innerHTML;",
                 params={"lot": lot})
    # 修前只有「原因：…；纠正措施：…（处理人）」；时刻照全站惯例截到分钟
    shown = row["handled_at"][:16].replace("T", " ")
    assert (f"原因：{HANDLE['reason']}；纠正措施：{HANDLE['corrective_action']}"
            f"（{row['handled_by']}，{shown} 处理）") in detail, detail
