"""工程包 I1：法定上报导出——传染病报告卡与死因报告卡（单卡 JSON + 批量 CSV + 权限/留痕）。"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import AccessLog


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "报卡县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


def make_user(client, admin, username, role, org_id):
    client.post(
        "/api/users",
        json={"username": username, "password": "pw12345678", "role": role, "org_id": org_id},
        headers=admin,
    )
    return login(client, username, "pw12345678")


@pytest.fixture(scope="module")
def director(client, admin, org):
    return make_user(client, admin, "card_director", "director", org["id"])


@pytest.fixture(scope="module")
def operator(client, admin, org):
    return make_user(client, admin, "card_operator", "operator", org["id"])


# ---------------------------------------------------------------------------
# 传染病报告卡
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cases(client, admin, org):
    late = client.post(
        "/api/infectious/cases",
        json={"org_id": org["id"], "disease_code": "A15", "disease_name": "肺结核",
              "onset_date": "2026-08-01"},  # 乙类 24h，隔了很多天 → 迟报
        headers=admin,
    ).json()
    ontime = client.post(
        "/api/infectious/cases",
        json={"org_id": org["id"], "disease_code": "J11", "disease_name": "流行性感冒",
              "onset_date": "2100-01-01"},  # 报告日早于发病日 → 不迟报
        headers=admin,
    ).json()
    unknown = client.post(
        "/api/infectious/cases",
        json={"org_id": org["id"], "disease_code": "X99", "disease_name": "目录外病种",
              "onset_date": "2026-08-01"},
        headers=admin,
    ).json()
    return late, ontime, unknown


def test_infectious_card_full_fields_and_late_linkage(client, admin, director, org, cases):
    late, _, unknown = cases
    card = client.get(f"/api/infectious/cases/{late['id']}/report-card", headers=director)
    assert card.status_code == 200, card.text
    body = card.json()
    # 法定字段集完整（平台留存字段，不虚构）
    assert body == {
        "case_id": late["id"],
        "org_id": org["id"],
        "org_name": "报卡县医院",
        "disease_code": "A15",
        "disease_name": "肺结核",
        "category": "B",
        "category_name": "乙类",
        "onset_date": "2026-08-01",
        "reported_at": body["reported_at"],
        "report_hours": 24,
        "days_late": body["days_late"],
        "late": True,
    }
    # 与未及时上报清单（/late-reports）口径联动：同一病例、同一迟报天数
    late_list = client.get("/api/infectious/late-reports", headers=admin).json()
    linked = next(r for r in late_list if r["case_id"] == late["id"])
    assert linked["days_late"] == body["days_late"]

    # 目录外病种：及时性三字段为 null，不假装能判
    unknown_card = client.get(
        f"/api/infectious/cases/{unknown['id']}/report-card", headers=director
    ).json()
    assert unknown_card["report_hours"] is None and unknown_card["late"] is None
    assert unknown_card["category_name"] == "目录外"

    assert client.get("/api/infectious/cases/999999/report-card", headers=director).status_code == 404


def test_infectious_csv_export_and_late_only(client, director, cases):
    late, ontime, _ = cases
    resp = client.get("/api/infectious/cases/export.csv", headers=director)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    assert "卡片编号" in text and "法定时限(小时)" in text
    assert "肺结核" in text and "迟报" in text
    assert "流行性感冒" in text and "及时" in text
    # 未及时上报清单联动：late_only 只导迟报卡
    late_csv = client.get("/api/infectious/cases/export.csv?late_only=true", headers=director).text
    assert "肺结核" in late_csv and "流行性感冒" not in late_csv


def test_infectious_export_requires_director(client, operator, cases):
    late, _, _ = cases
    assert client.get("/api/infectious/cases/export.csv", headers=operator).status_code == 403
    assert (
        client.get(f"/api/infectious/cases/{late['id']}/report-card", headers=operator).status_code
        == 403
    )
    assert client.get("/api/infectious/cases/export.csv").status_code == 401


# ---------------------------------------------------------------------------
# 死因报告卡
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def death_cert(client, admin, org):
    patient = client.post(
        "/api/patients",
        json={"name": "亡故者", "id_card": "330281194001013216", "gender": "男",
              "birth_date": "1940-01-01", "phone": "13800009999"},
        headers=admin,
    ).json()
    cert = client.post(
        "/api/certs",
        json={"cert_type": "death", "name": "亡故者", "gender": "男",
              "event_date": "2026-08-15", "detail": "冠心病急性心肌梗死",
              "org_id": org["id"], "patient_id": patient["id"]},
        headers=admin,
    ).json()
    return patient, cert


def test_death_card_fields_masked_and_logged(client, director, org, death_cert):
    patient, cert = death_cert
    resp = client.get(f"/api/certs/{cert['id']}/death-report-card", headers=director)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cert_no"] == cert["cert_no"]
    assert body["name"] == "亡故者" and body["death_date"] == "2026-08-15"
    assert body["cause_of_death"] == "冠心病急性心肌梗死"
    assert body["birth_date"] == "1940-01-01"
    assert body["org_name"] == "报卡县医院" and body["issued_by"] == "admin"
    # H1：非 admin 一律掩码，明文身份证号不得出现
    assert body["id_card"] == "3302**********3216"
    assert body["phone"] == "138******99"
    assert "330281194001013216" not in resp.text

    # 患者维度留痕：AccessLog 落 death_report_card 调阅记录
    with SessionLocal() as db:
        logged = (
            db.query(AccessLog)
            .filter(
                AccessLog.patient_id == patient["id"],
                AccessLog.resource == "death_report_card",
            )
            .count()
        )
    assert logged >= 1

    # 非死亡证明没有死因报告卡
    birth = client.post(
        "/api/certs",
        json={"cert_type": "birth", "name": "新生儿", "gender": "女",
              "event_date": "2026-08-16", "org_id": org["id"]},
        headers=login(client, "admin", "admin123"),
    ).json()
    assert (
        client.get(f"/api/certs/{birth['id']}/death-report-card", headers=director).status_code
        == 422
    )
    assert client.get("/api/certs/999999/death-report-card", headers=director).status_code == 404


def test_death_cards_csv_export(client, admin, director, death_cert):
    _, cert = death_cert
    resp = client.get("/api/certs/death-report-cards/export.csv", headers=director)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert cert["cert_no"] in resp.text and "冠心病急性心肌梗死" in resp.text
    # director 导出的 CSV 里身份证号同样掩码
    assert "3302**********3216" in resp.text and "330281194001013216" not in resp.text
    # admin 导出保留明文（数据导出场景，审计留痕）
    admin_csv = client.get("/api/certs/death-report-cards/export.csv", headers=admin).text
    assert "330281194001013216" in admin_csv
    # 日期过滤：范围外为空表
    empty = client.get(
        "/api/certs/death-report-cards/export.csv?date_from=2030-01-01", headers=director
    ).text
    assert cert["cert_no"] not in empty


def test_death_card_requires_director(client, operator, death_cert):
    _, cert = death_cert
    assert (
        client.get(f"/api/certs/{cert['id']}/death-report-card", headers=operator).status_code == 403
    )
    assert client.get("/api/certs/death-report-cards/export.csv", headers=operator).status_code == 403
