"""出生证明、死亡证明、出生缺陷儿登记的日期不得晚于今天；缺陷登记挂了患者时不得早于出生（P2-1538，第四十五批「门诊
西药发药与法定医学证明」扫描 AI3-2）。

修前 `CertCreate.event_date` 只查格式，只有死亡证明拿出生日期当下界（P2-940）：日期 2027-04-23 的出生证明、死亡证明、
缺陷登记都 201——死亡日期敲成明年，按死亡日期筛的死因报告卡月度导出本月导不到，要到那个月才出现，等于漏报；2026-09-20
出生的患者挂一张 2026-01-01 的缺陷登记照样 201。

修法：三类都 `event_date > 今天` 回 422「…不得晚于今天」（与发病日期 P2-454、接种日期 P2-1304、分娩日期 P2-1305 同一句），
日期的叫法与打印件同一套（出生日期 / 死亡日期 / 事件日期，P2-1242）；缺陷登记挂了患者时同样用 `before_birth_problem` 拿出生
日期当下界。出生证明与所挂档案出生日期的核对随 P2-1330 / P2-435 待裁定，不在本条。
"""
from datetime import timedelta

import pytest

from conftest import business_today

INFANT = {"name": "P21538 婴儿", "id_card": "330106202605011538", "gender": "女", "birth_date": "2026-05-01"}
ELDER = {"name": "P21538 老人", "id_card": "330106194003031538", "gender": "男", "birth_date": "1940-03-03"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21538 妇幼保健院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for key, person in (("infant", INFANT), ("elder", ELDER)):
        made = client.post("/api/patients", headers=admin, json=person)
        assert made.status_code in (200, 201), made.text
        ids[key] = made.json()["id"]
    return {"org": org, **ids}


def _issue(client, admin, world, cert_type, day, patient=None):
    body = {"cert_type": cert_type, "name": "P21538 证明", "gender": "女", "event_date": day,
            "detail": {"birth": "", "death": "呼吸循环衰竭", "defect": "先天性心脏病"}[cert_type],
            "org_id": world["org"]}
    if patient is not None:
        body["patient_id"] = world[patient]
    return client.post("/api/certs", headers=admin, json=body)


@pytest.mark.parametrize(("cert_type", "label", "patient"), [
    ("birth", "出生日期", None), ("death", "死亡日期", "elder"), ("defect", "事件日期", None),
], ids=["出生证明", "死亡证明", "缺陷登记"])
def test_三类证明日期晚于今天_422_今天照签(client, admin, world, cert_type, label, patient):
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    future = _issue(client, admin, world, cert_type, tomorrow, patient)
    assert future.status_code == 422, future.text   # 修前 201
    assert future.json()["detail"] == f"{label}（{tomorrow}）不得晚于今天"
    today = _issue(client, admin, world, cert_type, business_today().isoformat(), patient)
    assert today.status_code == 201, today.text


def test_缺陷登记挂了患者_早于出生422_出生当天照收(client, admin, world):
    early = _issue(client, admin, world, "defect", "2026-01-01", "infant")
    assert early.status_code == 422, early.text   # 修前 201
    assert early.json()["detail"] == "事件日期（2026-01-01）早于出生日期（2026-05-01）"
    assert _issue(client, admin, world, "defect", "2026-05-01", "infant").status_code == 201
    # 不挂患者的照旧只查不晚于今天：没有出生日期可比
    assert _issue(client, admin, world, "defect", "2026-01-01").status_code == 201


def test_死亡早于出生照旧422(client, admin, world):
    early = _issue(client, admin, world, "death", "1930-03-03", "elder")
    assert early.status_code == 422, early.text
    assert early.json()["detail"] == "死亡日期（1930-03-03）早于出生日期（1940-03-03）"   # P2-940 的原句，字节不变
