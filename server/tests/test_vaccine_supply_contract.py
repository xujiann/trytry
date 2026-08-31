"""疫苗批次/冷链/AEFI `vaccine_supply`（13 端点）的**响应契约**特征化网。

场景经 HTTP API 种出：自建机构与受种者、三个批次（在效/已过期/零库存）、
一针接种（走 /api/vaccination/records 扣库存）、两条冷链记录（正常/超温）、
两份 AEFI（关联剂次的一般反应 + 未关联的严重反应）。每个端点断言**完整精确
JSON**（dict 相等），代表性端点另钉**键序**。

三处易错的建模判断，在这里用数据钉死：

1. **`hint` 是条件键**：录温回执只在超温时多这个键，未超温时键**整个不出现**
   （不是 null）——`response_model_exclude_unset=True`，两条分支各钉一遍。
2. `temperature` 是 **Float 列**（整数入参读回来是 `5.0`）；AEFI 两个十万剂次
   发生率是"真除法 或 null"——**恒 float | None**，分母为 0 时明说无接种（null）
   而不是报 0。
3. 批次三查的 `unusable_reason` 三种原因各自成文（过期/封存/无库存），
   与 `usable` 一起按 today 现算，不靠定时任务改状态。
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/vaccine-supply"
TODAY = date.today().isoformat()
EXPIRE_SOON = (date.today() + timedelta(days=20)).isoformat()   # 30 天内到期
EXPIRE_FAR = (date.today() + timedelta(days=60)).isoformat()
EXPIRED = (date.today() - timedelta(days=1)).isoformat()

BATCH_KEYS = ["id", "vaccine_code", "vaccine_name", "batch_no", "manufacturer",
              "expire_date", "org_id", "quantity", "used_quantity", "remaining",
              "status", "frozen_reason", "expired", "usable", "unusable_reason"]
COLD_KEYS = ["id", "org_id", "device_name", "temperature", "range", "exceeded",
             "recorded_at", "handled", "handle_note"]
AEFI_KEYS = ["id", "patient_id", "record_id", "vaccine_code", "batch_no",
             "reaction_type", "reaction_type_name", "symptom", "onset_date",
             "outcome", "outcome_name", "org_id"]


def _iso(value: str) -> str:
    datetime.fromisoformat(value)
    return value


def _batch(bid, code, name, batch_no, expire, org_id, qty, used=0, status="normal",
           frozen_reason="", manufacturer="契约生物"):
    expired = expire < TODAY
    remaining = qty - used
    return {
        "id": bid, "vaccine_code": code, "vaccine_name": name, "batch_no": batch_no,
        "manufacturer": manufacturer, "expire_date": expire, "org_id": org_id,
        "quantity": qty, "used_quantity": used, "remaining": remaining,
        "status": status, "frozen_reason": frozen_reason, "expired": expired,
        "usable": (not expired) and status == "normal" and remaining > 0,
        "unusable_reason": ("已过效期" if expired else "已封存" if status != "normal"
                            else "库存已用完" if remaining <= 0 else ""),
    }


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "契约疫苗卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    v1 = client.post(
        "/api/patients",
        json={"name": "疫苗受种者一", "id_card": "330881199505051111", "gender": "女",
              "birth_date": "1995-05-05"},
        headers=h,
    ).json()
    v2 = client.post(
        "/api/patients",
        json={"name": "疫苗受种者二", "id_card": "330881200006062222", "gender": "男",
              "birth_date": "2000-06-06"},
        headers=h,
    ).json()
    return {"org": org, "v1": v1, "v2": v2}


# ------------------------------------------------- 批次


def test_批次登记与三查口径(client, h, base):
    org_id = base["org"]["id"]
    resp = client.post(
        f"{B}/batches",
        json={"vaccine_code": "HPV9", "vaccine_name": "九价HPV疫苗",
              "batch_no": "LOT-A1", "manufacturer": "契约生物",
              "expire_date": EXPIRE_SOON, "org_id": org_id, "quantity": 10},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    b1 = resp.json()
    assert list(b1) == BATCH_KEYS
    assert b1 == _batch(b1["id"], "HPV9", "九价HPV疫苗", "LOT-A1", EXPIRE_SOON,
                        org_id, 10)
    base["b1"] = b1

    b2 = client.post(
        f"{B}/batches",
        json={"vaccine_code": "FLU4", "vaccine_name": "四价流感疫苗",
              "batch_no": "LOT-B2", "manufacturer": "契约生物",
              "expire_date": EXPIRED, "org_id": org_id, "quantity": 5},
        headers=h,
    ).json()
    assert b2 == _batch(b2["id"], "FLU4", "四价流感疫苗", "LOT-B2", EXPIRED, org_id, 5)
    assert (b2["expired"], b2["usable"], b2["unusable_reason"]) == (True, False, "已过效期")
    base["b2"] = b2

    b3 = client.post(
        f"{B}/batches",
        json={"vaccine_code": "HPV9", "vaccine_name": "九价HPV疫苗",
              "batch_no": "LOT-C3", "manufacturer": "契约生物",
              "expire_date": EXPIRE_FAR, "org_id": org_id, "quantity": 0},
        headers=h,
    ).json()
    assert (b3["usable"], b3["unusable_reason"]) == (False, "库存已用完")
    base["b3"] = b3

    rows = client.get(f"{B}/batches", headers=h).json()
    assert [list(r) for r in rows] == [BATCH_KEYS] * 3
    assert rows == [b3, b2, b1]
    assert client.get(f"{B}/batches", params={"usable_only": True},
                      headers=h).json() == [b1]
    assert client.get(f"{B}/batches", params={"vaccine_code": "FLU4"},
                      headers=h).json() == [b2]


def test_封存与解封(client, h, base):
    b1 = base["b1"]
    frozen = client.post(f"{B}/batches/{b1['id']}/freeze",
                         json={"frozen_reason": "冷链超温待核查"}, headers=h)
    assert frozen.status_code == 200
    assert frozen.json() == {**b1, "status": "frozen", "frozen_reason": "冷链超温待核查",
                             "usable": False, "unusable_reason": "已封存"}
    assert client.post(f"{B}/batches/{b1['id']}/freeze",
                       json={"frozen_reason": "再封"}, headers=h).status_code == 409

    unfrozen = client.post(f"{B}/batches/{b1['id']}/unfreeze", headers=h)
    assert unfrozen.json() == b1  # 解封后与登记时逐字节一致


def test_批号反查受种者(client, h, base):
    org_id, b1 = base["org"]["id"], base["b1"]
    record = client.post(
        "/api/vaccination/records",
        json={"patient_id": base["v1"]["id"], "vaccine_code": "HPV9",
              "vaccine_name": "九价HPV疫苗", "dose_no": 1, "vaccinated_date": TODAY,
              "org_id": org_id, "batch_id": b1["id"]},
        headers=h,
    ).json()
    base["record"] = record

    body = client.get(f"{B}/batches/{b1['id']}/recipients", headers=h).json()
    assert list(body) == ["batch_no", "vaccine_name", "total", "recipients"]
    assert body == {
        "batch_no": "LOT-A1", "vaccine_name": "九价HPV疫苗", "total": 1,
        "recipients": [{"record_id": record["id"], "patient_id": base["v1"]["id"],
                        "patient_name": "疫苗受种者一", "dose_no": 1,
                        "vaccinated_date": TODAY, "org_id": org_id}],
    }
    # 接种扣减台账联动：批次余量随之减一
    base["b1"] = {**b1, "used_quantity": 1, "remaining": 9}
    assert client.get(f"{B}/batches", params={"vaccine_code": "HPV9",
                                              "usable_only": True},
                      headers=h).json() == [base["b1"]]


# ------------------------------------------------- 冷链


def test_冷链录温与处置(client, h, base):
    org_id = base["org"]["id"]
    normal = client.post(
        f"{B}/cold-chain",
        json={"org_id": org_id, "device_name": "1号冷藏箱", "temperature": 5,
              "recorded_at": f"{TODAY} 08:00:00"},
        headers=h,
    )
    assert normal.status_code == 201, normal.text
    c1 = normal.json()
    # 未超温：hint 键**整个不出现**（条件键），9 个键
    assert list(c1) == COLD_KEYS
    assert c1 == {"id": c1["id"], "org_id": org_id, "device_name": "1号冷藏箱",
                  "temperature": 5.0, "range": "2.0~8.0℃", "exceeded": False,
                  "recorded_at": f"{TODAY} 08:00:00", "handled": False,
                  "handle_note": ""}
    # Float 列：整数入参读回来是 5.0
    assert isinstance(c1["temperature"], float)
    base["c1"] = c1

    hot = client.post(
        f"{B}/cold-chain",
        json={"org_id": org_id, "device_name": "1号冷藏箱", "temperature": 12.5,
              "recorded_at": f"{TODAY} 09:30:00"},
        headers=h,
    )
    c2 = hot.json()
    assert list(c2) == COLD_KEYS + ["hint"]
    assert c2 == {"id": c2["id"], "org_id": org_id, "device_name": "1号冷藏箱",
                  "temperature": 12.5, "range": "2.0~8.0℃", "exceeded": True,
                  "recorded_at": f"{TODAY} 09:30:00", "handled": False,
                  "handle_note": "",
                  "hint": "已超出允许区间，请核查该设备内疫苗批次并决定是否封存（平台不自动封存）"}
    c2_row = {k: v for k, v in c2.items() if k != "hint"}
    base["c2"] = c2_row

    rows = client.get(f"{B}/cold-chain", headers=h).json()
    assert [list(r) for r in rows] == [COLD_KEYS] * 2
    assert rows == [c2_row, c1]
    assert client.get(f"{B}/cold-chain", params={"exceeded_only": True},
                      headers=h).json() == [c2_row]
    assert client.get(f"{B}/cold-chain", params={"unhandled_only": True},
                      headers=h).json() == [c2_row]

    handled = client.post(f"{B}/cold-chain/{c2['id']}/handle",
                          json={"handle_note": "已核查批次并转移疫苗"}, headers=h)
    assert handled.json() == {**c2_row, "handled": True,
                              "handle_note": "已核查批次并转移疫苗"}
    assert client.get(f"{B}/cold-chain", params={"unhandled_only": True},
                      headers=h).json() == []
    base["c2"] = handled.json()


# ------------------------------------------------- AEFI


def test_AEFI上报与转归(client, h, base):
    org_id = base["org"]["id"]
    linked = client.post(
        f"{B}/aefi",
        json={"patient_id": base["v1"]["id"], "record_id": base["record"]["id"],
              "symptom": "接种部位红肿", "onset_date": TODAY, "org_id": org_id},
        headers=h,
    )
    assert linked.status_code == 201, linked.text
    a1 = linked.json()
    assert list(a1) == AEFI_KEYS
    # 关联剂次：疫苗与批号从接种记录带出，不由上报人自报
    assert a1 == {"id": a1["id"], "patient_id": base["v1"]["id"],
                  "record_id": base["record"]["id"], "vaccine_code": "HPV9",
                  "batch_no": "LOT-A1", "reaction_type": "general",
                  "reaction_type_name": "一般反应", "symptom": "接种部位红肿",
                  "onset_date": TODAY, "outcome": "unknown", "outcome_name": "未知",
                  "org_id": org_id}
    base["a1"] = a1

    severe = client.post(
        f"{B}/aefi",
        json={"patient_id": base["v2"]["id"], "vaccine_code": "FLU4",
              "reaction_type": "severe", "symptom": "过敏性休克", "onset_date": TODAY,
              "outcome": "improving", "org_id": org_id},
        headers=h,
    ).json()
    assert severe == {"id": severe["id"], "patient_id": base["v2"]["id"],
                      "record_id": None, "vaccine_code": "FLU4", "batch_no": "",
                      "reaction_type": "severe", "reaction_type_name": "严重反应",
                      "symptom": "过敏性休克", "onset_date": TODAY,
                      "outcome": "improving", "outcome_name": "好转中",
                      "org_id": org_id}
    base["a2"] = severe

    rows = client.get(f"{B}/aefi", headers=h).json()
    assert [list(r) for r in rows] == [AEFI_KEYS] * 2
    assert rows == [severe, a1]
    assert client.get(f"{B}/aefi", params={"severe_only": True},
                      headers=h).json() == [severe]
    assert client.get(f"{B}/aefi", params={"batch_no": "LOT-A1"},
                      headers=h).json() == [a1]

    recovered = client.patch(f"{B}/aefi/{a1['id']}/outcome",
                             json={"outcome": "recovered"}, headers=h)
    assert recovered.json() == {**a1, "outcome": "recovered", "outcome_name": "痊愈"}
    base["a1"] = recovered.json()


# ------------------------------------------------- 统计与临期


def test_统计口径(client, h, base):
    body = client.get(f"{B}/stats", headers=h).json()
    assert list(body) == ["period", "group_id", "doses", "aefi", "batches",
                          "cold_chain", "caliber"]
    assert list(body["aefi"]) == ["total", "severe", "by_reaction",
                                  "rate_per_100k_doses", "severe_rate_per_100k_doses"]
    assert body == {
        "period": {"start": "不限", "end": "不限"},
        "group_id": None,
        "doses": 1,
        "aefi": {
            "total": 2, "severe": 1,
            "by_reaction": {"general": {"count": 1, "name": "一般反应"},
                            "severe": {"count": 1, "name": "严重反应"}},
            "rate_per_100k_doses": 200000.0,
            "severe_rate_per_100k_doses": 100000.0,
        },
        # 过期与封存按当下现算：B2 已过期；B1 二十天后到期（30 天窗口内）
        "batches": {"total": 3, "expired": 1, "frozen": 0, "expiring_soon": 1},
        "cold_chain": {"exceeded": 1, "exceeded_unhandled": 0},
        "caliber": {
            "aefi_rate": "分母为同期接种剂次（非人数）；无接种时返回 null 而非 0，"
                         "避免被读成零发生率",
            "batch_status": "过期与封存均按当下现算，不设定时任务改状态",
        },
    }
    assert isinstance(body["aefi"]["rate_per_100k_doses"], float)
    assert isinstance(body["aefi"]["severe_rate_per_100k_doses"], float)

    # 分母为 0：不报 0（会被读成零发生率），而是 null
    future = (date.today() + timedelta(days=5)).isoformat()
    empty = client.get(f"{B}/stats", params={"start_date": future}, headers=h).json()
    assert empty["period"] == {"start": future, "end": "不限"}
    assert empty["doses"] == 0
    assert empty["aefi"] == {"total": 0, "severe": 0, "by_reaction": {},
                             "rate_per_100k_doses": None,
                             "severe_rate_per_100k_doses": None}


def test_临期与过期清单(client, h, base):
    body = client.get(f"{B}/expiring", headers=h).json()
    assert list(body) == ["today", "within_days", "batches", "generated_at"]
    # 按效期升序：已过期的 B2 也列出（提示报废，防止误用）；零库存的 B3 不列
    assert body == {
        "today": TODAY, "within_days": 30,
        "batches": [base["b2"], base["b1"]],
        "generated_at": _iso(body["generated_at"]),
    }
    wide = client.get(f"{B}/expiring", params={"days": 365}, headers=h).json()
    assert wide["batches"] == [base["b2"], base["b1"]]  # B3 remaining=0 仍被滤掉


# ------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, h, base):
    org_id = base["org"]["id"]
    cases = [
        client.post(f"{B}/batches", headers=h,
                    json={"vaccine_code": "X", "vaccine_name": "X", "batch_no": "X",
                          "expire_date": TODAY, "org_id": 999999}),
        client.post(f"{B}/batches", headers=h,
                    json={"vaccine_code": "HPV9", "vaccine_name": "九价HPV疫苗",
                          "batch_no": "LOT-A1", "expire_date": EXPIRE_SOON,
                          "org_id": org_id, "quantity": 3}),   # 重复批号 → 409
        client.post(f"{B}/batches/999999/freeze", headers=h,
                    json={"frozen_reason": "x"}),
        client.post(f"{B}/batches/{base['b3']['id']}/unfreeze", headers=h),  # 未封存 → 409
        client.get(f"{B}/batches/999999/recipients", headers=h),
        client.post(f"{B}/cold-chain", headers=h,
                    json={"org_id": org_id, "device_name": "x", "temperature": 5,
                          "min_allowed": 8, "max_allowed": 2,
                          "recorded_at": f"{TODAY} 08:00:00"}),  # 区间颠倒 → 422
        client.post(f"{B}/cold-chain/999999/handle", headers=h,
                    json={"handle_note": "x"}),
        client.post(f"{B}/cold-chain/{base['c1']['id']}/handle", headers=h,
                    json={"handle_note": "x"}),                 # 未超温 → 422
        client.post(f"{B}/aefi", headers=h,
                    json={"patient_id": 999999, "vaccine_code": "X", "symptom": "x",
                          "onset_date": TODAY, "org_id": org_id}),
        client.post(f"{B}/aefi", headers=h,
                    json={"patient_id": base["v1"]["id"], "record_id": 999999,
                          "symptom": "x", "onset_date": TODAY, "org_id": org_id}),
        client.post(f"{B}/aefi", headers=h,
                    json={"patient_id": base["v2"]["id"],
                          "record_id": base["record"]["id"], "symptom": "x",
                          "onset_date": TODAY, "org_id": org_id}),  # 记录不属于该患者 → 422
        client.post(f"{B}/aefi", headers=h,
                    json={"patient_id": base["v1"]["id"], "symptom": "x",
                          "onset_date": TODAY, "org_id": org_id}),  # 缺疫苗编码 → 422
        client.patch(f"{B}/aefi/999999/outcome", headers=h,
                     json={"outcome": "recovered"}),
    ]
    assert [r.status_code for r in cases] == [404, 409, 404, 409, 404, 422, 404, 422,
                                              404, 404, 422, 422, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
