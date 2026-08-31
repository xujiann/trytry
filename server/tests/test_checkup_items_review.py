"""体检分项结果与总检流程（B2）：分项录入、异常标志口径、总检角色与响应兼容。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def ctx(client):
    admin = login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "分项体检医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "分项李四", "id_card": "330281199105057014", "gender": "女"},
        headers=admin,
    ).json()
    for username, role in (("cki_ph", "public_health"), ("cki_doc", "doctor")):
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    return {
        "admin": admin,
        "org": org,
        "patient": patient,
        "ph": login(client, "cki_ph", "pass123456"),
        "doc": login(client, "cki_doc", "pass123456"),
    }


ITEMS = [
    {"item_code": "ALT", "item_name": "丙氨酸氨基转移酶", "result_value": "72",
     "unit": "U/L", "ref_range": "9-50", "abnormal": True},
    {"item_code": "GLU", "item_name": "空腹血糖", "result_value": "5.2",
     "unit": "mmol/L", "ref_range": "3.9-6.1", "abnormal": False},
]


def test_分项录入_异常标志由分项触发_响应键不变(ctx, client):
    resp = client.post(
        "/api/checkups",
        json={"patient_id": ctx["patient"]["id"], "org_id": ctx["org"]["id"],
              "exam_date": "2026-07-01", "items": ITEMS},
        headers=ctx["ph"],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 汇总 abnormal_items 为空，但有分项异常 → has_abnormal 置真
    assert body["has_abnormal"] is True
    # 响应键与分项扩展前完全一致（不带 items，向后兼容）
    assert set(body.keys()) == {
        "patient_id", "org_id", "package_name", "exam_date",
        "summary", "abnormal_items", "id", "has_abnormal",
    }
    ctx["checkup_id"] = body["id"]


def test_分项查询返回逐项与异常标志(ctx, client):
    rows = client.get(f"/api/checkups/{ctx['checkup_id']}/items", headers=ctx["ph"]).json()
    assert [(r["item_code"], r["abnormal"]) for r in rows] == [("ALT", True), ("GLU", False)]
    assert rows[0]["result_value"] == "72"
    assert rows[0]["ref_range"] == "9-50"
    assert all(r["checkup_id"] == ctx["checkup_id"] for r in rows)


def test_不带分项的存量口径不变(ctx, client):
    resp = client.post(
        "/api/checkups",
        json={"patient_id": ctx["patient"]["id"], "org_id": ctx["org"]["id"],
              "exam_date": "2026-07-02", "summary": "各项正常"},
        headers=ctx["ph"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_abnormal"] is False
    assert client.get(
        f"/api/checkups/{resp.json()['id']}/items", headers=ctx["ph"]
    ).json() == []


def test_总检限医师_公卫403(ctx, client):
    resp = client.post(
        f"/api/checkups/{ctx['checkup_id']}/review",
        json={"final_conclusion": "肝功能异常，建议消化内科随诊复查"},
        headers=ctx["ph"],
    )
    assert resp.status_code == 403


def test_总检写入结论与医师署名(ctx, client):
    resp = client.post(
        f"/api/checkups/{ctx['checkup_id']}/review",
        json={"final_conclusion": "肝功能异常，建议消化内科随诊复查"},
        headers=ctx["doc"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["final_conclusion"].startswith("肝功能异常")
    assert body["final_doctor"] == "cki_doc"  # 未指名时以登录医师署名
    # 复核改结论：按覆盖
    again = client.post(
        f"/api/checkups/{ctx['checkup_id']}/review",
        json={"final_conclusion": "复核：转氨酶轻度升高，两周后复查", "final_doctor": "王主任"},
        headers=ctx["doc"],
    ).json()
    assert again["final_doctor"] == "王主任"


def test_不存在的体检记录404(ctx, client):
    assert client.get("/api/checkups/99999/items", headers=ctx["ph"]).status_code == 404
    assert client.post(
        "/api/checkups/99999/review",
        json={"final_conclusion": "x"},
        headers=ctx["doc"],
    ).status_code == 404
