"""医生 360 的体检段点名异常项：只在分项上标了异常的体检不再是空串（P2-1197，第三十四批「患者全景与时间轴」扫描 L4-5）。

体检的异常口径是「汇总异常串非空 **或** 任一分项异常」（`checkups.create_checkup`），汇总串选填。P2-422 给体检清单、
异常清单与打印件补了 `abnormal_text`（汇总串没写时列出标了异常的分项），漏了 360：修前 360 体检段只原样回显
`abnormal_items`，血红蛋白 95 g/L 这一分项标了异常的那次体检是 `{"has_abnormal": true, "abnormal_items": ""}`——
医生看到「有异常」，看不出哪项异常。

修后体检段只加 `abnormal_text`（复用 `checkups.abnormal_text` / `_abnormal_item_names`，与体检清单同一口径），
`abnormal_items` 照旧原样回显录入的汇总串。
"""
import pytest


def _ok(resp):
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture(scope="module")
def world(client, admin):
    org = _ok(client.post("/api/organizations", headers=admin, json={
        "name": "P21197 镇卫生院", "org_type": "township", "level": "township"}))["id"]
    patient = _ok(client.post("/api/patients", headers=admin, json={
        "name": "P21197 王阿姨", "id_card": "330102196502021197"}))
    common = {"patient_id": patient["id"], "org_id": org}
    hb_low = {"item_code": "HB", "item_name": "血红蛋白", "result_value": "95", "unit": "g/L",
              "ref_range": "115-150", "abnormal": True}
    glucose_ok = {"item_code": "GLU", "item_name": "空腹血糖", "result_value": "5.1", "unit": "mmol/L",
                  "ref_range": "3.9-6.1", "abnormal": False}
    exams = {
        # 只在分项上标了异常（汇总串没写）：修前 360 里就是一个空串
        "items_only": _ok(client.post("/api/checkups", headers=admin, json={
            **common, "exam_date": "2026-09-01", "items": [glucose_ok, hb_low]}))["id"],
        # 汇总串写了：以汇总串为准（与清单、打印件同一口径）
        "summary": _ok(client.post("/api/checkups", headers=admin, json={
            **common, "exam_date": "2026-08-01", "abnormal_items": "血压偏高", "items": [hb_low]}))["id"],
        "normal": _ok(client.post("/api/checkups", headers=admin, json={
            **common, "exam_date": "2026-07-01", "items": [glucose_ok]}))["id"],
    }
    view = client.get(f"/api/archive/{patient['ehc_no']}", headers=admin)
    assert view.status_code == 200, view.text
    listed = _ok(client.get("/api/checkups", headers=admin, params={"patient_id": patient["id"]}))
    return {"exams": exams, "rows": {row["id"]: row for row in view.json()["physical_exams"]},
            "listed": {row["id"]: row for row in listed}}


def test_只在分项上标了异常_360点名这一项(world):
    row = world["rows"][world["exams"]["items_only"]]
    assert row["has_abnormal"] is True
    assert row["abnormal_text"] == "血红蛋白"   # 修前没有这个键，只有下面这个空串
    assert row["abnormal_items"] == ""   # 既有键照旧原样回显录入的汇总串


def test_写了汇总串的以汇总串为准_无异常的是空串(world):
    assert world["rows"][world["exams"]["summary"]]["abnormal_text"] == "血压偏高"
    assert world["rows"][world["exams"]["summary"]]["abnormal_items"] == "血压偏高"
    assert world["rows"][world["exams"]["normal"]]["abnormal_text"] == ""


def test_与体检清单同一口径(world):
    for exam_id in world["exams"].values():
        assert world["rows"][exam_id]["abnormal_text"] == world["listed"][exam_id]["abnormal_text"], exam_id
