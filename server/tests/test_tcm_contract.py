"""中医药服务 `/api/tcm` 平台侧 9 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py` / `test_inpatient_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 5 个端点（dispense-orders×3 / techniques×2）
不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **体质辨识结果的七个键全部恒在**：`{"constitution", "score",
  "transformed_scores", "tendencies"}` 加上 `**CONSTITUTIONS[key]` 展开的
  `name`/`advice`/`formula`——九种体质的字典字面量键序一致，平和质只是
  `formula` 为空串，**没有条件键**，不用 exclude_unset。
- **`score`/`transformed_scores` 恒 int**：两条产地（直报经 `dict[str, int]`
  入参模型、简表经 `round()`）都是整数；声明 float 会把 70 印成 70.0。
- **制剂批次数量是 Integer 列**（`tcm_preparation_batches.quantity`），
  声明 `int` 并用 `type(x) is int` 钉；`expired` 是服务端按业务日期现算的
  bool，声明成 int 会把 true 印成 1。
- `_formula_out` / `_batch_out` 是各自唯一产地：新建回执与列表行同形，
  各建一个模型全端点共用；`expire_date` 留空推算与显式指定两条分支各钉一遍。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

SPEC_KEYS = ["method", "item_scoring", "raw_score", "transformed_score", "judge", "constitutions"]
CONSTITUTION_KEYS = [
    "constitution", "score", "transformed_scores", "tendencies", "name", "advice", "formula",
]
RECOMMENDATION_KEYS = ["syndrome", "matched", "match_count", "formula", "techniques"]
FORMULA_KEYS = [
    "id", "code", "name", "dosage_form", "dosage_form_name", "composition",
    "process", "indication", "shelf_life_months", "active",
]
BATCH_KEYS = [
    "id", "formula_id", "batch_no", "org_id", "quantity", "unit",
    "produced_date", "expire_date", "status", "expired",
]

TODAY = date.today()
#: 在产批次：10 天前投产，配方效期 6 个月（180 天）→ 效期在约 170 天后
PRODUCED_FRESH = (TODAY - timedelta(days=10)).isoformat()
EXPIRE_FRESH = (TODAY - timedelta(days=10) + timedelta(days=180)).isoformat()
#: 过期批次：400 天前投产 → 效期在约 220 天前
PRODUCED_OLD = (TODAY - timedelta(days=400)).isoformat()
EXPIRE_OLD = (TODAY - timedelta(days=400) + timedelta(days=180)).isoformat()


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言。

    配方：F1 全字段颗粒剂（效期 6 个月）、F2 全默认（汤剂/12 个月）。
    批次挂在 F1 上：B1 显式效期 2099-12-31、B2 效期留空按配方推算（+180 天）、
    B3 四百天前投产（已过期）。B1 之后发放成功，B3 发放被效期拦住。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约中医医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("tcct_pha", "pharmacist"), ("tcct_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        data[role] = login(client, username, "pass123456")

    resp = client.post(
        "/api/tcm/formulas",
        json={"code": "TCCT-1", "name": "契约益气颗粒", "dosage_form": "granule",
              "composition": "黄芪30g，党参15g", "process": "水提浓缩制粒",
              "indication": "气虚乏力", "shelf_life_months": 6},
        headers=data["pharmacist"],
    )
    assert resp.status_code == 201, resp.text
    data["f1"] = resp.json()
    data["f2"] = client.post(
        "/api/tcm/formulas", json={"code": "TCCT-2", "name": "契约默认合剂"},
        headers=data["pharmacist"],
    ).json()

    def batch(payload):
        resp = client.post(
            "/api/tcm/preparation-batches",
            json={"formula_id": data["f1"]["id"], "org_id": org["id"], **payload},
            headers=data["pharmacist"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["b1"] = batch({"batch_no": "TCCT-B1", "quantity": 100, "unit": "袋",
                        "produced_date": PRODUCED_FRESH, "expire_date": "2099-12-31"})
    data["b2"] = batch({"batch_no": "TCCT-B2", "quantity": 50,
                        "produced_date": PRODUCED_FRESH})  # 效期留空→按配方 6 个月推算
    data["b3"] = batch({"batch_no": "TCCT-B3", "quantity": 30,
                        "produced_date": PRODUCED_OLD})  # 已过期
    data["b1_released"] = client.post(
        f"/api/tcm/preparation-batches/{data['b1']['id']}/release?today={TODAY.isoformat()}",
        headers=data["operator"],
    ).json()
    return data


# ---------------------------------------------------------------- ⑬ 智能辅诊


def test_计分说明精确_键序(client, admin):
    body = client.get("/api/tcm/constitution/spec", headers=admin).json()
    assert list(body.keys()) == SPEC_KEYS
    assert list(body["judge"].keys()) == ["positive", "tendency", "balanced"]
    assert [list(c.keys()) for c in body["constitutions"]] == [["key", "name"]] * 9
    assert body == {
        "method": "中医体质分类与判定标准化简表",
        "item_scoring": "每一体质维度若干条目，按症状出现频度 1-5 分（没有=1 … 总是=5）逐条计分",
        "raw_score": "原始分 = 该维度各条目得分之和",
        "transformed_score": "转化分 = (原始分 - 条目数) / (条目数 × 4) × 100",
        "judge": {
            "positive": "偏颇体质转化分 ≥ 40 判定为该体质",
            "tendency": "转化分 30-39 判定为倾向体质",
            "balanced": "各偏颇体质转化分均 < 40 判定为平和质",
        },
        "constitutions": [
            {"key": "qi_deficiency", "name": "气虚质"},
            {"key": "yang_deficiency", "name": "阳虚质"},
            {"key": "yin_deficiency", "name": "阴虚质"},
            {"key": "phlegm_damp", "name": "痰湿质"},
            {"key": "damp_heat", "name": "湿热质"},
            {"key": "blood_stasis", "name": "血瘀质"},
            {"key": "qi_stagnation", "name": "气郁质"},
            {"key": "special", "name": "特禀质"},
            {"key": "balanced", "name": "平和质"},
        ],
    }


def test_体质辨识回执精确_偏颇与平和两分支(client, admin):
    body = client.post(
        "/api/tcm/constitution",
        json={"scores": {"damp_heat": 70, "blood_stasis": 35, "qi_deficiency": 10}},
        headers=admin,
    ).json()
    assert list(body.keys()) == CONSTITUTION_KEYS
    assert body == {
        "constitution": "湿热质",
        "score": 70,
        "transformed_scores": {"damp_heat": 70, "blood_stasis": 35, "qi_deficiency": 10},
        "tendencies": ["血瘀质"],
        "name": "湿热质",
        "advice": "清热利湿，忌烟酒辛辣肥甘；食疗：绿豆、苦瓜、马齿苋",
        "formula": "甘露消毒丹",
    }
    # 两条产地都是 int（直报经 dict[str,int] 入参、简表经 round）：70 不得变 70.0
    assert type(body["score"]) is int
    assert all(type(v) is int for v in body["transformed_scores"].values())

    balanced = client.post(
        "/api/tcm/constitution", json={"scores": {"damp_heat": 20}}, headers=admin
    ).json()
    assert list(balanced.keys()) == CONSTITUTION_KEYS  # 平和质同形：formula 空串而不是键消失
    assert balanced == {
        "constitution": "平和质",
        "score": 20,
        "transformed_scores": {"damp_heat": 20},
        "tendencies": [],
        "name": "平和质",
        "advice": "起居有常，饮食有节，坚持运动",
        "formula": "",
    }


def test_体质辨识简表计分分支_未知维度被滤掉(client, admin):
    body = client.post(
        "/api/tcm/constitution",
        json={"answers": {"damp_heat": [5, 5, 5, 5], "qi_deficiency": [1, 1, 1, 1],
                          "not_a_key": [3]}},
        headers=admin,
    ).json()
    assert body == {
        "constitution": "湿热质",
        "score": 100,  # (20-4)/(4×4)×100，round 后是 int
        "transformed_scores": {"damp_heat": 100, "qi_deficiency": 0},
        "tendencies": [],
        "name": "湿热质",
        "advice": "清热利湿，忌烟酒辛辣肥甘；食疗：绿豆、苦瓜、马齿苋",
        "formula": "甘露消毒丹",
    }
    assert type(body["score"]) is int


def test_智能辨证回执精确_命中与空两分支(client, admin):
    body = client.post(
        "/api/tcm/assist-diagnosis", json={"symptoms": ["乏力", "气短", "口干"]}, headers=admin
    ).json()
    assert list(body.keys()) == ["recommendations", "note"]
    assert [list(r.keys()) for r in body["recommendations"]] == [RECOMMENDATION_KEYS] * 2
    assert body == {
        "recommendations": [
            {"syndrome": "气虚证", "matched": ["乏力", "气短"], "match_count": 2,
             "formula": "四君子汤", "techniques": ["艾灸足三里", "穴位贴敷"]},
            {"syndrome": "阴虚证", "matched": ["口干"], "match_count": 1,
             "formula": "六味地黄丸", "techniques": ["耳穴压豆"]},
        ],
        "note": "辅助建议仅供参考，须由中医师最终辨证",
    }
    assert type(body["recommendations"][0]["match_count"]) is int
    miss = client.post(
        "/api/tcm/assist-diagnosis", json={"symptoms": ["查无此症"]}, headers=admin
    ).json()
    assert miss == {"recommendations": [], "note": "辅助建议仅供参考，须由中医师最终辨证"}


# ---------------------------------------------------------------- ⑭ 制剂配方


def test_配方回执精确_键序与默认值(client, admin, seed):
    body = seed["f1"]
    assert list(body.keys()) == FORMULA_KEYS
    assert body == {
        "id": body["id"],
        "code": "TCCT-1",
        "name": "契约益气颗粒",
        "dosage_form": "granule",
        "dosage_form_name": "颗粒剂",
        "composition": "黄芪30g，党参15g",
        "process": "水提浓缩制粒",
        "indication": "气虚乏力",
        "shelf_life_months": 6,
        "active": True,
    }
    assert type(body["shelf_life_months"]) is int
    # 全默认的那份：汤剂/12 个月/空串——默认值也是契约的一部分
    assert seed["f2"] == {
        "id": seed["f2"]["id"], "code": "TCCT-2", "name": "契约默认合剂",
        "dosage_form": "decoction", "dosage_form_name": "合剂/汤剂", "composition": "",
        "process": "", "indication": "", "shelf_life_months": 12, "active": True,
    }


def test_配方列表与回执同形_过滤(client, admin, seed):
    rows = client.get("/api/tcm/formulas", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [FORMULA_KEYS] * 2  # id 倒序
    assert rows == [seed["f2"], seed["f1"]]
    assert client.get("/api/tcm/formulas?active=true", headers=admin).json() == rows
    assert client.get("/api/tcm/formulas?active=false", headers=admin).json() == []


# ---------------------------------------------------------------- ⑭ 制剂批次


def test_批次回执精确_显式效期与推算效期(seed):
    body = seed["b1"]
    assert list(body.keys()) == BATCH_KEYS
    assert body == {
        "id": body["id"],
        "formula_id": seed["f1"]["id"],
        "batch_no": "TCCT-B1",
        "org_id": seed["org"]["id"],
        "quantity": 100,
        "unit": "袋",
        "produced_date": PRODUCED_FRESH,
        "expire_date": "2099-12-31",
        "status": "produced",
        "expired": False,
    }
    # Integer 列：产量 100 不得变 100.0；expired 是 bool，不得变 0/1
    assert type(body["quantity"]) is int
    assert body["expired"] is False
    # 效期留空 → 按配方 6 个月（180 天）推算；单位默认"剂"
    assert seed["b2"] == {
        "id": seed["b2"]["id"], "formula_id": seed["f1"]["id"], "batch_no": "TCCT-B2",
        "org_id": seed["org"]["id"], "quantity": 50, "unit": "剂",
        "produced_date": PRODUCED_FRESH, "expire_date": EXPIRE_FRESH,
        "status": "produced", "expired": False,
    }
    # 投产即已过期的批次：回执上的 expired 按当天现算
    assert seed["b3"]["expire_date"] == EXPIRE_OLD and seed["b3"]["expired"] is True


def test_批次列表与回执同形_过滤(client, admin, seed):
    released_b1 = {**seed["b1"], "status": "released"}
    rows = client.get(
        f"/api/tcm/preparation-batches?today={TODAY.isoformat()}", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [BATCH_KEYS] * 3  # id 倒序
    assert rows == [seed["b3"], seed["b2"], released_b1]
    assert client.get(
        f"/api/tcm/preparation-batches?status=produced&today={TODAY.isoformat()}",
        headers=admin,
    ).json() == [seed["b3"], seed["b2"]]
    assert client.get(
        f"/api/tcm/preparation-batches?formula_id={seed['f2']['id']}", headers=admin
    ).json() == []


def test_效期预警列表精确_按效期升序(client, admin, seed):
    rows = client.get(
        f"/api/tcm/preparation-batches/expiring?days=30&today={TODAY.isoformat()}",
        headers=admin,
    ).json()
    assert rows == [seed["b3"]]  # 只有过期批次落进 30 天窗口
    rows = client.get(
        f"/api/tcm/preparation-batches/expiring?days=200&today={TODAY.isoformat()}",
        headers=admin,
    ).json()
    assert [list(r.keys()) for r in rows] == [BATCH_KEYS] * 2  # 效期先后排序
    assert rows == [seed["b3"], seed["b2"]]


def test_发放回执与批次回执同形(seed):
    body = seed["b1_released"]
    assert list(body.keys()) == BATCH_KEYS
    assert body == {**seed["b1"], "status": "released"}


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    pha = seed["pharmacist"]
    ok_batch = {"formula_id": seed["f1"]["id"], "org_id": seed["org"]["id"],
                "batch_no": "TCCT-ERR", "quantity": 1, "produced_date": PRODUCED_FRESH}
    cases = [
        client.post("/api/tcm/constitution", json={}, headers=admin),  # 两种入参都缺 422
        client.post("/api/tcm/constitution",
                    json={"scores": {"damp_heat": 150}}, headers=admin),  # 越界 422
        client.post("/api/tcm/constitution",
                    json={"answers": {"damp_heat": [0, 6]}}, headers=admin),  # 条目分越界 422
        client.post("/api/tcm/constitution",
                    json={"scores": {"balanced": 50}}, headers=admin),  # 无有效维度 422
        client.post("/api/tcm/formulas",
                    json={"code": "TCCT-1", "name": "重复"}, headers=pha),  # 409
        client.post("/api/tcm/preparation-batches",
                    json={**ok_batch, "formula_id": 999999}, headers=pha),  # 404
        client.post("/api/tcm/preparation-batches",
                    json={**ok_batch, "batch_no": "TCCT-B1"}, headers=pha),  # 批号重复 409
        client.post("/api/tcm/preparation-batches",
                    json={**ok_batch, "expire_date": PRODUCED_FRESH}, headers=pha),  # 效期≤投产 422
        client.post(f"/api/tcm/preparation-batches/{seed['b3']['id']}/release"
                    f"?today={TODAY.isoformat()}", headers=pha),  # 已过效期 409
        client.post(f"/api/tcm/preparation-batches/{seed['b1']['id']}/release"
                    f"?today={TODAY.isoformat()}", headers=pha),  # 已发放再发放 409
        client.post("/api/tcm/preparation-batches/999999/release", headers=pha),  # 404
    ]
    assert [r.status_code for r in cases] == [422, 422, 422, 422, 409, 404, 409, 422, 409, 409, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
