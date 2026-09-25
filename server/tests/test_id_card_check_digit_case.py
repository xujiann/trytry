"""身份证号末位校验码 X 的大小写：同一个人不许因此在主索引里有两份档案（P1-114）。

GB 11643 的校验码是大写 X，手输（尤其手机键盘、对接方各自的录入习惯）常见小写 x；校验码由前 17 位算出，
两种写法必是同一个人。可主索引（EMPI）「同证件号返回既有档案」是按录入原样等值查的，唯一约束也区分大小写：

- 2026-09-25 实测（修前代码，开发库与真 PG 同样）：先按 `…002X` 建档、再按 `…002x` 建档，两次都 201，
  **两个健康卡号、两份档案**——同一个人的就诊、检验、慢病管理从此分散在两处；
- 对接入站同病：HL7 A04 按另一种写法建出第二份，A08 信息更新 / A03 出院按另一种写法 404「档案不存在」；
- 真 PG 上检索也分叉：`LIKE` 区分大小写，按 `x` 搜只见小写那份、按 `X` 搜只见大写那份（开发库 SQLite 的
  `LIKE` 对 ASCII 不分大小写，两份都列出来——开发、测试一律看着正常）。

修法：`patients.id_card_variants` / `id_card_match`——查证件号时两种写法都认（明文与加密两态都走
`pii_filter`），存量按录入原样不动；建档查重、对接入站三处、患者检索、慢专病两处按证件号检索都换上它。
居民端身份核验 / 绑定与凭证核验按原样等值，属认证链路，按 CLAUDE.md §8 登记待复核，不在本批改。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 用 `MEDPLAT_IDCASE_PG_URL`
把本文件换到 PG 上再跑一遍。
"""
import os

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前（同 test_body_numeric_capacity）
_PG_URL = os.environ.get("MEDPLAT_IDCASE_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402

# GB 11643 合法、末位校验码为 X 的号（前 17 位 330192198001010xx）
UPPER_A = "33019219800101008X"
UPPER_B = "33019219800101016X"
UPPER_C = "33019219800101024X"
UPPER_D = "33019219800101032X"
UPPER_E = "33019219800101040X"


def _create(client, admin, name, id_card):
    resp = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _adt(event, control_id, id_card, name):
    return (f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260925090000||{event}|{control_id}|P|2.4\r"
            f"PID|1||{id_card}^^^CN^ID||{name}||19800101|M|||杭州市||13800001114")


def _hl7(client, admin, message):
    return client.post("/api/integration/hl7v2/adt", json={"message": message}, headers=admin)


# ================================================================ 写法变体本身
@pytest.mark.parametrize("raw, expected", [
    ("33019219800101008X", ["33019219800101008X", "33019219800101008x"]),
    ("33019219800101008x", ["33019219800101008x", "33019219800101008X"]),
    ("330192198001010081", ["330192198001010081"]),
    ("330192800101008", ["330192800101008"]),   # 15 位旧号没有校验码
    ("", [""]),
])
def test_写法变体只在末位X上分叉(raw, expected):
    from app.routers.patients import id_card_variants

    assert id_card_variants(raw) == expected


# ================================================================ 主索引建档查重
def test_先大写后小写_建档返回同一份档案(client, admin):
    first = _create(client, admin, "P1114 甲", UPPER_A)
    again = _create(client, admin, "P1114 甲（小写录入）", UPPER_A[:-1] + "x")
    assert again["id"] == first["id"] and again["ehc_no"] == first["ehc_no"]   # 修前：第二份档案


def test_先小写后大写_建档返回同一份档案(client, admin):
    first = _create(client, admin, "P1114 乙", UPPER_B[:-1] + "x")
    again = _create(client, admin, "P1114 乙（大写录入）", UPPER_B)
    assert again["id"] == first["id"]


def test_对照_不同证件号照常各建一份(client, admin):
    a = _create(client, admin, "P1114 丙", UPPER_C)
    b = _create(client, admin, "P1114 丁", UPPER_D)
    assert a["id"] != b["id"]


# ================================================================ 对接入站
def test_HL7_A04另一种写法不建第二份_A08按另一种写法照常更新(client, admin):
    first = _hl7(client, admin, _adt("ADT^A04", "P1114-1", UPPER_E, "P1114 戊"))
    assert first.status_code == 201 and first.json()["created"] is True, first.text
    again = _hl7(client, admin, _adt("ADT^A04", "P1114-2", UPPER_E[:-1] + "x", "P1114 戊"))
    assert again.json()["created"] is False, again.text   # 修前 True：第二份档案
    update = _hl7(client, admin, _adt("ADT^A08", "P1114-3", UPPER_E[:-1] + "x", "P1114 戊改"))
    assert update.status_code == 201, update.text          # 修前 404「档案不存在」（ADT 一律 201 回 ACK）
    assert update.json()["detail"] == "患者信息已更新"
    assert update.json()["patient"]["id"] == first.json()["patient"]["id"]


# ================================================================ 检索（真 PG 上 LIKE 区分大小写）
def test_按另一种写法检索也找得到(client, admin):
    stored_upper = _create(client, admin, "P1114 己", "33019219800101059X")
    found = client.get("/api/patients", headers=admin, params={"keyword": "33019219800101059x"})
    assert found.status_code == 200, found.text
    assert stored_upper["id"] in {p["id"] for p in found.json()}   # 修前真 PG：查不到


def test_慢专病纳管名单按另一种写法检索也找得到(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1114 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p1114_prog", "name": "证件号写法病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text
    patient = _create(client, admin, "P1114 庚", "33019219800101067X")
    enroll = client.post("/api/spd/enrollments", headers=admin,
                         json={"patient_id": patient["id"], "program_code": "p1114_prog", "org_id": org})
    assert enroll.status_code == 201, enroll.text
    found = client.get("/api/spd/enrollments", headers=admin, params={"keyword": "33019219800101067x"})
    assert found.status_code == 200, found.text
    assert enroll.json()["id"] in {e["id"] for e in found.json()}   # 修前真 PG：查不到
