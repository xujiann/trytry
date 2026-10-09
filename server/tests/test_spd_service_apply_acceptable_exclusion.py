"""服务申请清单的 `acceptable` 没跟上 P2-935：命中病种排除规则的待受理申请照画「受理」，点了 409（P2-1575，第四十六批扫描 AJ3-5）。

P2-796 让清单行带 `acceptable`（注释写「与 `handle_service_apply` 同一判据」），那时受理只判病种停用；P2-935 又给受理加了病种排除
规则，清单没跟上。修前实测：14 岁居民的存量待受理申请（P2-935 之前递交）`acceptable` 为 true，页面照画「受理」，点下去 409「按病种
规则不纳入（未成年人不纳入成人高血压管理），只能驳回」。档案事实后来变了的（补了出生日期、新出 E10）同理。

修法：`acceptable` 再加一道 `exclusion_problem`，与受理同一判据；页面不能受理时，病种还在用的照受理 409 的说法写「按病种规则不纳入，
只能驳回」，病种停用的照旧写「病种已停用，只能驳回」。成年人的申请照常能受理。
"""
from datetime import date
from pathlib import Path

import pytest
from jssrc import strip_comments

from app.database import SessionLocal
from app.spd.models import SpdServiceApply
from conftest import business_today

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    teen_birth = date(business_today().year - 14, 1, 1).isoformat()   # 现取：满 14 岁
    applies = {}
    for key, name, id_card, birth in (("teen", "P21575 少年", "330106201201011575", teen_birth),
                                      ("adult", "P21575 成年", "330106197001011576", "1970-01-01")):
        made = client.post("/api/patients", headers=admin, json={
            "name": name, "id_card": id_card, "gender": "男", "birth_date": birth})
        assert made.status_code in (200, 201), made.text
        with SessionLocal() as db:   # 存量待受理申请：P2-935 之前递交的（居民端如今不收未成年人申请成人高血压管理）
            row = SpdServiceApply(patient_id=made.json()["id"], program_code="hypertension", note="想加入管理",
                                  status="pending")
            db.add(row)
            db.commit()
            applies[key] = row.id
    return applies


def _acceptable(client, admin, apply_id):
    rows = client.get(f"{B}/service-applies", headers=admin, params={"status": "pending", "limit": 200}).json()
    return next(r["acceptable"] for r in rows if r["id"] == apply_id)


def test_未成年人的存量申请_清单标不能受理_与受理一致(client, admin, world):
    assert _acceptable(client, admin, world["teen"]) is False   # 修前 True，页面照画「受理」
    resp = client.post(f"{B}/service-applies/{world['teen']}/handle", headers=admin, json={"status": "accepted"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "按病种规则不纳入（未成年人不纳入成人高血压管理），只能驳回"
    assert _acceptable(client, admin, world["teen"]) is False   # 仍待受理，仍不能受理


def test_成年人的申请照常能受理(client, admin, world):
    assert _acceptable(client, admin, world["adult"]) is True
    resp = client.post(f"{B}/service-applies/{world['adult']}/handle", headers=admin, json={"status": "accepted"})
    assert resp.status_code == 200, resp.text


def test_页面不能受理时按病种在不在用写原因():
    start = PAGE.index('${panel("居民服务申请（待受理）"')
    block = strip_comments(PAGE[start:PAGE.index('${panel("召回跟进"', start)])
    assert '${a.acceptable ? `<button class="btn secondary" data-apply="${a.id}" data-decision="accepted">受理</button>`' in block
    # 病种还在用却不能受理：命中了排除规则（修前一律写「病种已停用，只能驳回」）
    assert ('catalog.programs.some((p) => p.code === a.program_code && p.active) ? "按病种规则不纳入" : "病种已停用"}'
            '，只能驳回') in block
