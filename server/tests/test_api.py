import pytest

from conftest import login


@pytest.fixture(scope="module")
def auth_headers(client):
    return login(client, "admin", "admin123")


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_frontend_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "县域医共体信息化平台" in page.text
    assert client.get("/static/app.js").status_code == 200


def test_login_rejects_bad_password(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_protected_endpoint_requires_token(client):
    assert client.get("/api/organizations").status_code == 401


def test_organization_hierarchy(client, auth_headers):
    lead = client.post(
        "/api/organizations",
        json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"},
        headers=auth_headers,
    )
    assert lead.status_code == 201
    township = client.post(
        "/api/organizations",
        json={
            "name": "城东镇卫生院",
            "org_type": "township",
            "level": "township",
            "parent_id": lead.json()["id"],
        },
        headers=auth_headers,
    )
    assert township.status_code == 201

    dup = client.post(
        "/api/organizations",
        json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"},
        headers=auth_headers,
    )
    assert dup.status_code == 409

    listed = client.get("/api/organizations?level=township", headers=auth_headers)
    assert [o["name"] for o in listed.json()] == ["城东镇卫生院"]


def test_empi_registration_is_idempotent(client, auth_headers):
    body = {"name": "张三", "id_card": "320981199001011234", "gender": "男"}
    first = client.post("/api/patients", json=body, headers=auth_headers)
    assert first.status_code == 201
    ehc_no = first.json()["ehc_no"]
    assert ehc_no.startswith("EHC")

    second = client.post("/api/patients", json=body, headers=auth_headers)
    assert second.json()["ehc_no"] == ehc_no

    fetched = client.get(f"/api/patients/{ehc_no}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "张三"


def test_dictionary_four_unifications(client, auth_headers):
    entry = client.post(
        "/api/dictionaries/diagnosis/entries",
        json={"code": "TST.01", "name": "测试专用示例诊断"},
        headers=auth_headers,
    )
    assert entry.status_code == 201

    dup = client.post(
        "/api/dictionaries/diagnosis/entries",
        json={"code": "TST.01", "name": "重复"},
        headers=auth_headers,
    )
    assert dup.status_code == 409

    found = client.get(
        "/api/dictionaries/diagnosis/entries?keyword=测试专用示例", headers=auth_headers
    )
    assert len(found.json()) == 1
    # 块3：启动种子——常用 ICD-10 已入库（I10 等），关键词可检索
    seeded = client.get(
        "/api/dictionaries/diagnosis/entries?keyword=高血压", headers=auth_headers
    )
    assert any(e["code"] == "I10" for e in seeded.json())

    unknown = client.get("/api/dictionaries/unknown/entries", headers=auth_headers)
    assert unknown.status_code == 404


@pytest.fixture(scope="module")
def referral_fixtures(client, auth_headers):
    """本用例要用的机构与患者，自己建。

    原来是直接 `GET /api/patients?keyword=张三` 取上一条用例建的那个人、
    再 `GET /api/organizations` 取上一条用例建的机构——**跨用例借数据**。
    整模块跑得过，是因为前面那两条恰好先执行；`pytest -k referral` 只选中这一条时，
    列表是空的，`[0]` 直接 IndexError。测试之间不该有这种看不见的先后依赖。
    """
    patient = client.post(
        "/api/patients",
        json={"name": "转诊流转患者", "id_card": "320981199001011299", "gender": "男"},
        headers=auth_headers,
    ).json()
    orgs = {}
    for name, level, org_type in (
        ("转诊流转卫生院", "township", "township"),
        ("转诊流转县医院", "county", "lead_hospital"),
    ):
        resp = client.post(
            "/api/organizations",
            json={"name": name, "level": level, "org_type": org_type},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        orgs[level] = resp.json()
    return {"patient": patient, **orgs}


def test_referral_status_flow(client, auth_headers, referral_fixtures):
    patient = referral_fixtures["patient"]
    township = referral_fixtures["township"]
    county = referral_fixtures["county"]

    referral = client.post(
        "/api/referrals",
        json={
            "patient_id": patient["id"],
            "from_org_id": township["id"],
            "to_org_id": county["id"],
            "direction": "up",
            "reason": "疑难心电图，请上级判读后收治",
        },
        headers=auth_headers,
    )
    assert referral.status_code == 201
    rid = referral.json()["id"]
    assert referral.json()["status"] == "pending"

    # pending 不能直接结案
    bad = client.patch(
        f"/api/referrals/{rid}/status", json={"status": "completed"}, headers=auth_headers
    )
    assert bad.status_code == 409

    accepted = client.patch(
        f"/api/referrals/{rid}/status", json={"status": "accepted"}, headers=auth_headers
    )
    assert accepted.json()["status"] == "accepted"

    completed = client.patch(
        f"/api/referrals/{rid}/status", json={"status": "completed"}, headers=auth_headers
    )
    assert completed.json()["status"] == "completed"


def test_spa_covers_every_backend_module(client):
    """T6·B：后端每个业务模块都应有管理端入口，不能只存在于 /docs。

    这条断言是防回退的——新增路由模块却忘了加页面，会在这里失败。
    白名单里的模块是刻意不做管理端页面的（探活、对机器接口、居民端自有 H5）。
    """
    import re

    from app.main import app

    # 阶段十二：app.js 已按业务域拆成多个文件，这里读全部前端脚本。
    # 文件清单从 index.html 里取，而不是在测试里再写一份——两处清单迟早会不一致，
    # 而不一致的表现是"页面明明有、守卫却说没有"。
    index_html = client.get("/static/index.html").text
    scripts = re.findall(r'<script src="(/static/[^"]+\.js)"', index_html)
    assert scripts, "index.html 里没有找到任何前端脚本"
    js = "\n".join(client.get(src).text for src in scripts)
    called = set(re.findall(r"[\"'`]/api/([a-z0-9_-]+)", js))
    served = {
        p.split("/")[2]
        for p in app.openapi()["paths"]
        if len(p.split("/")) > 2 and p.split("/")[1] == "api"
    }
    no_admin_page = {
        "health",       # 探活
        "integration",  # HL7/FHIR，对机器不对人
        "portal",       # 居民端有自己的 H5
        "triage",       # 智能分诊由业务页内联调用
    }
    missing = sorted(served - called - no_admin_page)
    assert not missing, f"以下后端模块没有管理端页面：{missing}"


def test_spa_registers_phase_six_pages(client):
    """阶段一~五的模块都已在页面注册表里，且每个 id 有对应渲染函数。"""
    js = client.get("/static/app.js").text  # 注册表就在 app.js 里
    for page_id in ("clinicaldocs", "surgery", "followups", "accounting", "cost",
                    "materials", "analytics", "rules", "workflows", "jobs",
                    "servicerequests"):
        assert f'id: "{page_id}"' in js, f"缺少页面注册：{page_id}"
