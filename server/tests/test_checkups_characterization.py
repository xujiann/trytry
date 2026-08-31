"""特征化测试——保护 checkups `/abnormal` 端点从「裸 dict」迁移到「标准接口」。

    混乱代码（裸 dict，无 response_model）  →  标准接口（response_model 声明契约）

这条端点原本返回手拼 dict、OpenAPI 上无响应契约。迁移目标是给它加
`response_model`，但**响应字节必须一模一样**（向后兼容，CLAUDE.md 第7条）。
本测试钉住迁移前的精确输出形状——迁移后仍须全绿，即证明行为未变。

关键断言：`/abnormal` 每个元素的键**恰好**是 {id, patient_id, exam_date,
abnormal_items} 四个，不多不少。response_model 若少声明一个字段会把它从响应里
删掉、若混进模型的额外字段会多出来——两种都会让下面的断言变红。
"""
from __future__ import annotations

import pytest

from conftest import login


EXPECTED_KEYS = {"id", "patient_id", "exam_date", "abnormal_items"}


@pytest.fixture(scope="module")
def ctx(client):
    admin = login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "体检特征化医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "体检张三", "id_card": "330281199003034002", "gender": "男"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "chk_ph", "password": "pass123456", "role": "public_health", "org_id": org["id"]},
        headers=admin,
    )
    ph = login(client, "chk_ph", "pass123456")
    # 一条异常 + 一条正常，确认 /abnormal 只出异常那条
    client.post(
        "/api/checkups",
        json={"patient_id": patient["id"], "org_id": org["id"], "exam_date": "2026-05-01", "summary": "正常"},
        headers=ph,
    )
    ab = client.post(
        "/api/checkups",
        json={
            "patient_id": patient["id"], "org_id": org["id"], "exam_date": "2026-06-01",
            "summary": "血压偏高", "abnormal_items": "血压 150/95",
        },
        headers=ph,
    ).json()
    return {"ph": ph, "patient": patient, "org": org, "abnormal_id": ab["id"]}


def test_abnormal_列表键恰好为四个(ctx, client):
    rows = client.get("/api/checkups/abnormal", headers=ctx["ph"]).json()
    assert isinstance(rows, list) and rows, "至少应有一条异常记录"
    for row in rows:
        assert set(row.keys()) == EXPECTED_KEYS, f"键集合漂移：{set(row.keys())}"


def test_abnormal_只含异常记录且值正确(ctx, client):
    rows = client.get("/api/checkups/abnormal", headers=ctx["ph"]).json()
    mine = [r for r in rows if r["id"] == ctx["abnormal_id"]]
    assert len(mine) == 1
    row = mine[0]
    assert row["patient_id"] == ctx["patient"]["id"]
    assert row["exam_date"] == "2026-06-01"
    assert row["abnormal_items"] == "血压 150/95"


def test_abnormal_正常记录不出现(ctx, client):
    rows = client.get("/api/checkups/abnormal", headers=ctx["ph"]).json()
    # 正常那条 summary=正常、abnormal_items 空，has_abnormal=False，不该出现
    dates = {r["exam_date"] for r in rows}
    assert "2026-05-01" not in dates
