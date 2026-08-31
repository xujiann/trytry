"""M6-M7 移动端：居民端 H5 挂载与双因子满意度提交。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def admin_headers(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def patient(client, admin_headers):
    return client.post(
        "/api/patients",
        json={"name": "移动端居民", "id_card": "320981199407076666", "phone": "13100001111"},
        headers=admin_headers,
    ).json()


def test_mobile_page_served(client):
    page = client.get("/m")
    assert page.status_code == 200
    assert "居民服务" in page.text
    assert client.get("/static/m/m.js").status_code == 200
    assert client.get("/static/m/m.css").status_code == 200


def test_mobile_login_ui_present(client):
    """居民端登录改版：页面应提供微信登录与手机号验证码两条入口，且不再索取健康卡号。"""
    page = client.get("/m").text
    assert "微信一键登录" in page
    assert 'id="btn-send-code"' in page and 'id="in-phone"' in page
    assert 'id="bind-form"' in page  # 实名绑定
    assert "电子健康卡号" not in page


def test_mobile_service_tab_present(client):
    """在线服务页：家庭成员代管入口与预约/签约/账单/转诊四个分段。"""
    page = client.get("/m").text
    assert 'id="family-switch"' in page and 'id="family-form"' in page
    for svc in ("appointment", "contract", "bill", "referral"):
        assert f'data-svc="{svc}"' in page


def test_mobile_health_articles_public(client, admin_headers):
    """健康宣教：发布后无需登录即可在居民端读取。"""
    art = client.post(
        "/api/education/articles",
        json={"title": "高血压防治要点", "category": "chronic", "content": "限盐、运动、规律服药"},
        headers=admin_headers,
    )
    assert art.status_code == 201
    client.post(
        f"/api/education/articles/{art.json()['id']}/publish", headers=admin_headers
    )
    articles = client.get("/api/portal/health-articles").json()
    assert any(a["title"] == "高血压防治要点" for a in articles)


@pytest.fixture()
def legacy_verify_enabled(monkeypatch):
    """旧双因子接口默认已关（portal_legacy_verify=False，A2/P1-3）；
    这两条测的是接口本身的行为，显式开启后再测。"""
    from app.config import settings

    monkeypatch.setattr(settings, "portal_legacy_verify", True)


def test_portal_survey_two_factor(client, patient, legacy_verify_enabled):
    # 身份核验失败
    bad = client.post(
        "/api/portal/surveys",
        json={
            "ehc_no": patient["ehc_no"],
            "id_card": "WRONG",
            "target_type": "encounter",
            "score": 5,
        },
    )
    assert bad.status_code == 403

    ok = client.post(
        "/api/portal/surveys",
        json={
            "ehc_no": patient["ehc_no"],
            "id_card": patient["id_card"],
            "target_type": "encounter",
            "score": 4,
            "comment": "候诊时间略长",
        },
    )
    assert ok.status_code == 201
    assert ok.json()["submitted"] is True

    # 无效评分被拒
    invalid = client.post(
        "/api/portal/surveys",
        json={
            "ehc_no": patient["ehc_no"],
            "id_card": patient["id_card"],
            "target_type": "encounter",
            "score": 9,
        },
    )
    assert invalid.status_code == 422


def test_portal_my_archive_still_works(client, patient, legacy_verify_enabled):
    resp = client.get(
        f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card={patient['id_card']}"
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "移动端居民"
