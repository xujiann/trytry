"""检验室内质控（IQC，B2）：Westgard 基础四规则数值边界、失控处理闭环与 L-J 口径。

非空洞声明：`test_westgard_边界精确` 直接钉住 1-2s/1-3s/2-2s/R-4s 的数值阈值
（严格大于口径，±2.0/±3.0/Δ4.0 恰好不触发）——改动任一阈值本文件必红。
"""
import pytest

from conftest import login

from app.routers.labqc import _westgard


@pytest.fixture(scope="module")
def world(client, admin):
    """甲乙两院各配一名检验医师账号，甲院备一个基准批号（靶值 5.0 / SD 0.5）。"""
    orgs, docs = {}, {}
    for tag in ("甲", "乙"):
        org = client.post(
            "/api/organizations",
            json={"name": f"质控{tag}院", "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json()
        client.post(
            "/api/users",
            json={"username": f"iqc_{tag}", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
        orgs[tag], docs[tag] = org, login(client, f"iqc_{tag}", "pass123456")
    return {"orgs": orgs, "docs": docs}


def _mk_lot(client, world, lot_no, target=5.0, sd=0.5):
    resp = client.post(
        "/api/labqc/lots",
        json={
            "org_id": world["orgs"]["甲"]["id"], "item_code": "K", "item_name": "血清钾",
            "lot_no": lot_no, "target_value": target, "sd": sd,
        },
        headers=world["docs"]["甲"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _measure(client, world, lot_id, value, **extra):
    resp = client.post(
        f"/api/labqc/lots/{lot_id}/measurements",
        json={"value": value, **extra},
        headers=world["docs"]["甲"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- Westgard 判定函数的数值边界（非空洞：改阈值必红） ----------


def test_westgard_边界精确():
    # 1-2s：|z| 恰为 2.0 不警告，超出才警告
    assert _westgard(2.0, None) == (False, False, [])
    assert _westgard(-2.0, None) == (False, False, [])
    assert _westgard(2.05, None) == (True, False, [])
    # 1-3s：|z| 恰为 3.0 只算 1-2s 警告；超出即失控
    assert _westgard(3.0, None) == (True, False, [])
    assert _westgard(3.05, None) == (False, True, ["1-3s"])
    assert _westgard(-3.05, None) == (False, True, ["1-3s"])
    # 2-2s：连续两点同侧超 2SD；异侧不算
    assert _westgard(2.5, 2.5) == (False, True, ["2-2s"])
    assert _westgard(-2.5, -2.4) == (False, True, ["2-2s"])
    assert _westgard(2.5, 1.9) == (True, False, [])          # 上一点未超 2SD
    # R-4s：相邻极差恰为 4.0 不触发，超出才失控
    assert _westgard(2.0, -2.0) == (False, False, [])
    assert _westgard(2.5, -1.6) == (False, True, ["R-4s"])   # Δ=4.1
    assert _westgard(-2.2, 2.5) == (False, True, ["R-4s"])   # 异侧，2-2s 不命中
    # 同侧连续超 2SD 且极差不超：只报 2-2s
    assert _westgard(2.5, 2.1) == (False, True, ["2-2s"])


# ---------- 录入接口逐规则红绿 ----------


def test_在控与1_2s警告(client, world):
    lot = _mk_lot(client, world, "LOT-A")
    ok = _measure(client, world, lot["id"], 6.0)     # z=2.0：恰好压线，在控
    assert (ok["warning"], ok["out_of_control"]) == (False, False)
    warn = _measure(client, world, lot["id"], 6.1)   # z=2.2：1-2s 警告
    # 前点 z=2.0 未超 2SD，2-2s 不命中；Δz=0.2，R-4s 不命中——只剩警告
    assert (warn["warning"], warn["out_of_control"]) == (True, False)
    assert warn["violated_rules"] == ""


def test_1_3s失控(client, world):
    lot = _mk_lot(client, world, "LOT-B")
    edge = _measure(client, world, lot["id"], 6.5)   # z=3.0：不失控（严格大于）
    assert edge["out_of_control"] is False
    bad = _measure(client, world, lot["id"], 3.3)    # z=-3.4：1-3s 失控
    assert bad["out_of_control"] is True
    assert "1-3s" in bad["violated_rules"]


def test_2_2s连续两点同侧失控(client, world):
    lot = _mk_lot(client, world, "LOT-C")
    first = _measure(client, world, lot["id"], 6.25)   # z=2.5：单点只是警告
    assert (first["warning"], first["out_of_control"]) == (True, False)
    second = _measure(client, world, lot["id"], 6.3)   # z=2.6：连续同侧超 2SD
    assert second["out_of_control"] is True
    assert "2-2s" in second["violated_rules"]


def test_2_2s异侧不误报(client, world):
    lot = _mk_lot(client, world, "LOT-D")
    _measure(client, world, lot["id"], 6.25)               # z=+2.5
    other = _measure(client, world, lot["id"], 4.25)       # z=-1.5：Δ=4.0 恰不触发 R-4s
    assert other["out_of_control"] is False
    assert other["violated_rules"] == ""


def test_R_4s极差失控(client, world):
    lot = _mk_lot(client, world, "LOT-E")
    _measure(client, world, lot["id"], 6.25)               # z=+2.5
    swing = _measure(client, world, lot["id"], 3.9)        # z=-2.2：Δ=4.7 极差失控
    assert swing["out_of_control"] is True
    assert "R-4s" in swing["violated_rules"]
    assert "2-2s" not in swing["violated_rules"]           # 异侧，不算 2-2s


# ---------- 失控处理闭环 ----------


def test_失控未处理再录入给警示_处理后消除(client, world):
    lot = _mk_lot(client, world, "LOT-F")
    bad = _measure(client, world, lot["id"], 7.0)          # z=4.0：1-3s 失控
    assert bad["out_of_control"] is True
    assert bad["unhandled_before"] == 0                    # 此前无欠账
    nxt = _measure(client, world, lot["id"], 5.0)          # 失控未处理仍可录，但有警示
    assert nxt["unhandled_before"] == 1
    assert "未处理" in nxt["alert"]
    handled = client.post(
        f"/api/labqc/measurements/{bad['id']}/handle",
        json={"reason": "质控品复溶超时失效", "corrective_action": "更换新支质控品复测并重新定标"},
        headers=world["docs"]["甲"],
    )
    assert handled.status_code == 200, handled.text
    assert handled.json()["handled"] is True
    assert handled.json()["handled_by"]
    after = _measure(client, world, lot["id"], 5.1)
    assert after["unhandled_before"] == 0
    assert after["alert"] == ""


def test_在控点不可处理_失控点不可重复处理(client, world):
    lot = _mk_lot(client, world, "LOT-G")
    ok = _measure(client, world, lot["id"], 5.0)
    resp = client.post(
        f"/api/labqc/measurements/{ok['id']}/handle",
        json={"reason": "x", "corrective_action": "y"},
        headers=world["docs"]["甲"],
    )
    assert resp.status_code == 422
    bad = _measure(client, world, lot["id"], 7.0)
    for expected in (200, 409):  # 第一次登记成功，第二次 409
        resp = client.post(
            f"/api/labqc/measurements/{bad['id']}/handle",
            json={"reason": "仪器漂移", "corrective_action": "重新定标"},
            headers=world["docs"]["甲"],
        )
        assert resp.status_code == expected, resp.text


# ---------- L-J 数据口径 ----------


def test_LJ序列均值与SD线(client, world):
    lot = _mk_lot(client, world, "LOT-H", target=10.0, sd=1.0)
    for v in (10.0, 11.0, 7.5):
        _measure(client, world, lot["id"], v)
    lj = client.get(f"/api/labqc/lots/{lot['id']}/levey-jennings", headers=world["docs"]["甲"]).json()
    assert lj["lines"] == {
        "mean": 10.0, "sd1_upper": 11.0, "sd1_lower": 9.0,
        "sd2_upper": 12.0, "sd2_lower": 8.0, "sd3_upper": 13.0, "sd3_lower": 7.0,
    }
    assert [p["value"] for p in lj["points"]] == [10.0, 11.0, 7.5]  # 按录入顺序
    assert [p["z"] for p in lj["points"]] == [0.0, 1.0, -2.5]
    # z=-2.5 与前点 Δz=3.5：1-3s/2-2s/R-4s 均未中，仅 1-2s 警告
    assert lj["points"][2]["warning"] is True
    assert lj["points"][2]["out_of_control"] is False


def test_LJ点位判定与录入判定一致(client, world):
    lot = _mk_lot(client, world, "LOT-I", target=10.0, sd=1.0)
    _measure(client, world, lot["id"], 10.0)
    bad = _measure(client, world, lot["id"], 13.5)  # z=3.5 失控
    lj = client.get(f"/api/labqc/lots/{lot['id']}/levey-jennings", headers=world["docs"]["甲"]).json()
    point = [p for p in lj["points"] if p["id"] == bad["id"]][0]
    assert point["out_of_control"] is True
    assert point["violated_rules"] == bad["violated_rules"]
    assert point["handled"] is False


# ---------- 批号维护与越权 ----------


def test_批号重复409_SD非正422_停用后拒录(client, world):
    _mk_lot(client, world, "LOT-J")
    dup = client.post(
        "/api/labqc/lots",
        json={"org_id": world["orgs"]["甲"]["id"], "item_code": "K", "item_name": "血清钾",
              "lot_no": "LOT-J", "target_value": 5.0, "sd": 0.5},
        headers=world["docs"]["甲"],
    )
    assert dup.status_code == 409
    zero_sd = client.post(
        "/api/labqc/lots",
        json={"org_id": world["orgs"]["甲"]["id"], "item_code": "K", "item_name": "血清钾",
              "lot_no": "LOT-J2", "target_value": 5.0, "sd": 0},
        headers=world["docs"]["甲"],
    )
    assert zero_sd.status_code == 422
    lot = _mk_lot(client, world, "LOT-K")
    off = client.patch(
        f"/api/labqc/lots/{lot['id']}", json={"active": False}, headers=world["docs"]["甲"]
    )
    assert off.status_code == 200 and off.json()["active"] is False
    refused = client.post(
        f"/api/labqc/lots/{lot['id']}/measurements", json={"value": 5.0}, headers=world["docs"]["甲"]
    )
    assert refused.status_code == 409


def test_跨机构建批与录入403(client, world):
    lot = _mk_lot(client, world, "LOT-L")
    other = world["docs"]["乙"]
    assert client.post(
        "/api/labqc/lots",
        json={"org_id": world["orgs"]["甲"]["id"], "item_code": "K", "item_name": "血清钾",
              "lot_no": "LOT-X", "target_value": 5.0, "sd": 0.5},
        headers=other,
    ).status_code == 403
    assert client.post(
        f"/api/labqc/lots/{lot['id']}/measurements", json={"value": 5.0}, headers=other
    ).status_code == 403
    assert client.get(
        f"/api/labqc/lots/{lot['id']}/levey-jennings", headers=other
    ).status_code == 403
