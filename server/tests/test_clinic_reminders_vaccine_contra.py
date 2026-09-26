"""诊间提醒只提示生效中的疫苗禁忌：已解除、已过期的原先也照样提示（P2-132）。

禁忌的有效性口径早就写在模型注释与接种闸门里：`active=生效中, lifted=已解除`；过期（`valid_until` 已过，含当日）
不改状态、按日期现算——接种时只拦生效中的（`vaccination._effective_contraindications`）。诊间提醒却把这位患者的
禁忌一条不落地列出来：同一时刻接种台放行这支疫苗、医生的提醒里还挂着「禁忌」。

修法：诊间提醒按同一个判定取禁忌。
"""
import pytest

TODAY = "2026-09-26"


@pytest.fixture(scope="module")
def patient(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P2132 受种者", "id_card": "330106201805051320", "gender": "女", "birth_date": "2018-05-05"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _contra(client, admin, patient, code, **extra):
    resp = client.post("/api/vaccination/contraindications", headers=admin, json={
        "patient_id": patient, "vaccine_code": code, "reason": f"{code} 禁忌", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_只提示生效中的禁忌(client, admin, patient):
    _contra(client, admin, patient, "P2132-PERM")                                               # 永久：生效
    _contra(client, admin, patient, "P2132-SOON", contra_type="temporary", valid_until="2026-09-30")  # 未到期：生效
    _contra(client, admin, patient, "P2132-EDGE", contra_type="temporary", valid_until=TODAY)          # 末日当天：生效
    _contra(client, admin, patient, "P2132-PAST", contra_type="temporary", valid_until="2026-09-10")  # 已过期
    lifted = _contra(client, admin, patient, "P2132-LIFT")
    assert client.post(f"/api/vaccination/contraindications/{lifted}/lift", headers=admin,
                       json={"lift_reason": "复查正常"}).status_code == 200                    # 已解除

    body = client.get(f"/api/publichealth/reminders/{patient}?today={TODAY}", headers=admin).json()
    shown = sorted(r["detail"] for r in body["reminders"] if r["type"] == "vaccine_contraindication")
    assert shown == ["疫苗 P2132-EDGE 禁忌：P2132-EDGE 禁忌", "疫苗 P2132-PERM 禁忌：P2132-PERM 禁忌",
                     "疫苗 P2132-SOON 禁忌：P2132-SOON 禁忌"]   # 修前五条都在：已过期、已解除的照样提示
