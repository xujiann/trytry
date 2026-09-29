"""出生日期不得晚于今天；存量里的将来日期算年龄当「不知道」，不再算出负数（P2-713，第十八批「数值入参的符号与业务
上下界」扫描 V3-4）。

年龄全靠出生日期现算（审方的儿童 / 老年人群、未满 14 周岁须监护人、慢专病的年龄纳入 / 排除规则）。建档与更正只查
格式：把 1970 敲成 2070 建档 201，算出的年龄是 -44——慢专病「未满 18 岁不纳入」把这位成年人排除，知情同意代录要求
监护人。传染病报告的发病日期早就不许晚于今天（P2-454）。

修法：建档（`register_patient`）与更正（`_check_correction_value`）的出生日期不得晚于今天，422；两处 `_age_of`
（审方 / 同意书共用的一处、慢专病规则事实一处）算出负数返回 None，存量里的将来日期与写坏的日期同一个口径——当「不知道」。
"""
from datetime import date, timedelta

from app import clock


def test_建档出生日期晚于今天_422(client, admin):
    future = (clock.today() + timedelta(days=365 * 44)).isoformat()
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P2713 患者", "id_card": "330281197001012713", "birth_date": future})
    assert resp.status_code == 422, resp.text   # 修前 201，年龄算成 -44
    assert "不得晚于今天" in resp.json()["detail"]
    ok = client.post("/api/patients", headers=admin, json={
        "name": "P2713 患者", "id_card": "330281197001012713", "birth_date": clock.today().isoformat()})
    assert ok.status_code == 201, ok.text   # 今天出生照收


def test_更正出生日期为将来_422():
    import pytest
    from fastapi import HTTPException

    from app.routers.consents import validate_correction_changes

    future = (clock.today() + timedelta(days=1)).isoformat()
    with pytest.raises(HTTPException) as info:
        validate_correction_changes("correct", {"birth_date": future})
    assert info.value.status_code == 422 and "不得晚于今天" in str(info.value.detail)


def test_存量将来日期算年龄当不知道_不算出负数():
    from app.routers.prescriptions import _age_of as rx_age_of
    from app.spd.service import _age_of as spd_age_of

    future = (clock.today() + timedelta(days=365 * 44)).isoformat()
    assert rx_age_of(future) is None and spd_age_of(future) is None   # 修前 -44 / -43
    born = date(1970, 1, 1).isoformat()
    assert rx_age_of(born) == spd_age_of(born) and rx_age_of(born) > 50
