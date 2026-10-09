"""调阅授权的有效期不得早于今天（P2-1726，第五十一批扫描 AO2-7）。

`AuthorizationCreate.expire_date` 原先只查日期写法：有效期填 2025-10-08 照收 201、`status: active`，页面报「授权已登记」——
清单上它当场是「已过期」，校验接口 `allowed: False`，一天也不生效，而患者以为授权已经办好；有效期还是文本框，敲错年份更容易。
同类「填了已过去的日期」的知识库有效期（P2-1668）早已拒收。修后早于今天（`clock.today()`，与调阅判定
`visibility.active_authorization_grants` 同一把尺子）422「有效期不得早于今天」，当天照收、当天有效；页面有效期改成日期控件、
最早只能选本地今天。
"""
import shutil
from datetime import timedelta

import pytest
from conftest import business_today, freeze_business_date
from patients_page import run


@pytest.fixture(scope="module")
def world(client, admin):
    grantee = client.post("/api/organizations", headers=admin, json={
        "name": "P21726 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21726 患者", "id_card": "330106197404041726"}).json()["id"]
    return {"grantee": grantee, "patient": patient}


def _grant(client, admin, world, expire_date):
    return client.post(f"/api/patients/{world['patient']}/authorizations", headers=admin,
                       json={"grantee_org_id": world["grantee"], "scope": "all", "expire_date": expire_date})


def _rows(client, admin, world):
    return {r["id"]: r for r in client.get(f"/api/patients/{world['patient']}/authorizations", headers=admin).json()}


def test_有效期是昨天_422_不落库(client, admin, world):
    before = _rows(client, admin, world)
    yesterday = (business_today() - timedelta(days=1)).isoformat()
    resp = _grant(client, admin, world, yesterday)
    assert resp.status_code == 422, resp.text   # 修前 201、active，清单当场「已过期」
    assert resp.json()["detail"].startswith("有效期不得早于今天"), resp.json()
    assert _rows(client, admin, world).keys() == before.keys()


def test_有效期是今天_照收且当天有效(client, admin, world):
    resp = _grant(client, admin, world, business_today().isoformat())
    assert resp.status_code == 201, resp.text
    row = _rows(client, admin, world)[resp.json()["id"]]
    assert (row["effective"], row["status_name"]) == (True, "有效")
    check = client.get(f"/api/patients/{world['patient']}/authorizations/check", headers=admin,
                       params={"org_id": world["grantee"]}).json()
    assert check["allowed"] is True


def test_按业务日判_冻住的今天之前一天拒_当天收(client, admin, world):
    from datetime import date

    with freeze_business_date(date(2030, 3, 1)):
        assert _grant(client, admin, world, "2030-02-28").status_code == 422
        assert _grant(client, admin, world, "2030-03-01").status_code == 201


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_页面有效期是日期控件_最早只能选今天(client, admin):
    steps = """
      await renderPatients();
      return { html: pageHtml(), today: localToday() };
    """
    got, _ = run(client, admin, steps)
    form = got["html"][got["html"].index('<form class="inline" id="auth-grant-form">'):]
    form = form[:form.index("</form>")]
    assert f'<input name="expire_date" type="date" required min="{got["today"]}">' in form, form   # 修前是文本框
    assert "YYYY-MM-DD" not in form
