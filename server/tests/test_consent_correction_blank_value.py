"""档案更正的新值判空与建档同一判据：姓名只填零宽字符（U+200B）这类看不见的字一律 422（P2-1724，第五十一批扫描 AO2-5）。

`validate_correction_changes` 原先按 `str(value).strip()` 判空：U+200B、BOM（U+FEFF）这类格式字符不是空白，`strip()` 去不掉——
更正姓名只填一个 U+200B，提交 201、审批 200，档案姓名成了看不见的字，清单上是一位没有名字的人。建档的 `name` 带 `NON_BLANK`，
同样的值早就 422（P2-1148）。修后判空用 `texttypes.is_blank_text`（与 `NON_BLANK` 逐码点同一判据）；窗口代提与居民端提交走同一个
函数。修复前已提交、还在待审的这种申请，审批时 409、档案不动（与 P1-61 / P2-1723 的待审申请同一口径）。
"""
import json

import pytest
from fastapi import HTTPException

from app.database import SessionLocal
from app.models import CorrectionRequest
from app.routers.consents import validate_correction_changes

INVISIBLE = {"零宽空格": "​", "BOM": "﻿", "零宽夹空白": "​ 　⁠"}


@pytest.fixture(scope="module")
def patient(client, admin):
    made = client.post("/api/patients", headers=admin, json={"name": "P21724 甲", "id_card": "330782199001017240"})
    assert made.status_code == 201, made.text
    return made.json()


def _submit(client, admin, patient, changes):
    return client.post("/api/consents/corrections", headers=admin, json={
        "patient_id": patient["id"], "request_type": "correction", "changes": changes, "reason": "改名"})


@pytest.mark.parametrize("value", INVISIBLE.values(), ids=INVISIBLE.keys())
def test_更正姓名只填看不见的字_422(client, admin, patient, value):
    resp = _submit(client, admin, patient, {"name": value})
    assert resp.status_code == 422, resp.text   # 修前 201，审批 200 后档案姓名成了看不见的字
    assert resp.json()["detail"] == "更正字段 name 的新值不能为空"


def test_居民端与窗口共用的校验函数_同一判据():
    with pytest.raises(HTTPException) as caught:
        validate_correction_changes("correction", {"name": "​"})
    assert caught.value.status_code == 422
    # 有看得见的字、只是夹着零宽字符的，与 NON_BLANK 一样照收（不替人改值）
    assert validate_correction_changes("correction", {"name": "乙​"}) == '{"name": "乙​"}'


def test_建档同一个值也是422(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "​", "id_card": "330782199001017241"})
    assert resp.status_code == 422, resp.text


def test_正常更正照旧(client, admin, patient):
    resp = _submit(client, admin, patient, {"name": "P21724 乙"})
    assert resp.status_code == 201, resp.text
    reviewed = client.post(f"/api/consents/corrections/{resp.json()['id']}/review", headers=admin, json={"approve": True})
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved", reviewed.text
    assert client.get(f"/api/patients/{patient['ehc_no']}", headers=admin).json()["name"] == "P21724 乙"


def test_修复前已提交的看不见字更正_审批时409且档案不动(client, admin, patient):
    before = client.get(f"/api/patients/{patient['ehc_no']}", headers=admin).json()["name"]
    with SessionLocal() as db:
        legacy = CorrectionRequest(patient_id=patient["id"], request_type="correction",
                                   changes=json.dumps({"name": "\u200b"}), reason="修复前提交的申请", source="window")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    resp = client.post(f"/api/consents/corrections/{legacy_id}/review", headers=admin, json={"approve": True})
    assert resp.status_code == 409, resp.text   # 修前 200，档案姓名成了看不见的字
    assert resp.json()["detail"] == "该申请的更正字段 name 不合法（新值不能为空），请驳回后由申请人重新提交"
    assert client.get(f"/api/patients/{patient['ehc_no']}", headers=admin).json()["name"] == before
    with SessionLocal() as db:
        assert db.get(CorrectionRequest, legacy_id).status == "pending"
