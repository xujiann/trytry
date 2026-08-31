"""用户与审计 `/api/users|/api/audit` 六个待治理端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。本模块已治理的 8 个端点（UserOut 等）不在本网范围，仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- `/users/roles` 直接返回 `deps.ROLE_NAMES` 字典 → `dict[str, str]`，键序=字典
  插入序，此处逐键钉死。
- `/audit/verify` 是**一模型两形状**：空区间分支只出 `checked/valid/note`，
  非空分支出 `checked/legacy_unchained/from_id/to_id/partial_segment/valid/
  broken_at/reason/caliber`，锚点对账时两种分支都在**末尾**追加
  `anchor_id/anchor_match/anchor_reason` —— 全部条件键按出键序声明 +
  `response_model_exclude_unset=True`；`note` 声明在 `caliber` 之前
  （两键从不同场出现，声明序只需分别满足两分支的出键序）。
  `broken_at` 是**值可空的恒在键**（非空分支永远带着它，链完好时为 null）。
- `/audit/stats` 的 `failed_ratio_pct` 恒 float（`round(x*100, 2)` 与兜底字面量
  `0.0` 都是 float），不涉 Money。
- `/audit/export` 是 **NDJSON 流式下载**（`StreamingResponse`），`response_model`
  对它没有意义——照 reports.CsvResponse 的写法给一个自带 media_type 的
  Response 子类同时当 `response_class` 与实际返回类。本文件钉住逐行 JSON 与
  content-type/content-disposition。
- 时间字段（`at`）是 `created_at.isoformat()` 字符串，值随运行时刻变，
  钉键与类型、值从响应回绑。
"""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog

ROLES_KEY_ORDER = ["admin", "director", "doctor", "pharmacist", "public_health", "operator"]
AUDIT_ROW_KEYS = ["id", "username", "method", "path", "status_code", "at"]
VERIFY_FULL_KEYS = [
    "checked", "legacy_unchained", "from_id", "to_id", "partial_segment",
    "valid", "broken_at", "reason", "caliber",
]
ANCHOR_KEYS = ["anchor_id", "anchor_match", "anchor_reason"]
STATS_KEY_ORDER = [
    "days", "scope", "total", "failed", "failed_ratio_pct", "daily",
    "failed_status_codes", "top_users", "top_paths", "top_failed_paths",
]
ROLE_CHANGE_KEYS = ["id", "user_id", "old_role", "new_role", "changed_by", "at"]

CALIBER = (
    "哈希链能发现历史记录被改动；但拦不住有库权限且知道平台密钥者"
    "重算整条链——不可抵赖需外部存证或只追加存储"
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    """恰好三次写操作（201/200/409）——审计流水、统计与导出的全部素材。

    登录不落 audit_logs（审计中间件只记业务写），本模块此后只做 GET，
    所以三条就是全部：POST /api/users ×2（一次 409）+ PATCH role ×1。
    """
    created = client.post(
        "/api/users",
        json={"username": "uc_doc", "password": "passw0rd1", "role": "doctor"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    dup = client.post(
        "/api/users",
        json={"username": "uc_doc", "password": "passw0rd1", "role": "doctor"},
        headers=admin,
    )
    assert dup.status_code == 409
    changed = client.patch(
        f"/api/users/{created.json()['id']}/role", json={"role": "operator"}, headers=admin
    )
    assert changed.status_code == 200, changed.text
    return {"user": created.json()}


# ---------------------------------------------------------------- /users/roles


def test_角色字典精确形状与键序(client, admin):
    body = client.get("/api/users/roles", headers=admin).json()
    assert list(body.keys()) == ROLES_KEY_ORDER
    assert body == {
        "admin": "平台管理员",
        "director": "管理层",
        "doctor": "医师",
        "pharmacist": "药师",
        "public_health": "公卫人员",
        "operator": "经办人员",
    }


# ---------------------------------------------------------------- /audit（列表）


def test_审计流水精确形状与键序(client, admin, seeded):
    resp = client.get("/api/audit", headers=admin)
    rows = resp.json()
    assert resp.headers["x-total-count"] == "3"
    assert [list(r.keys()) for r in rows] == [AUDIT_ROW_KEYS] * 3
    uid = seeded["user"]["id"]
    # id 倒序；at 是 isoformat 字符串（值随运行时刻变，回绑后整 dict 相等）
    assert all(isinstance(r["at"], str) and "T" in r["at"] for r in rows)
    assert rows == [
        {"id": rows[0]["id"], "username": "admin", "method": "PATCH",
         "path": f"/api/users/{uid}/role", "status_code": 200, "at": rows[0]["at"]},
        {"id": rows[1]["id"], "username": "admin", "method": "POST",
         "path": "/api/users", "status_code": 409, "at": rows[1]["at"]},
        {"id": rows[2]["id"], "username": "admin", "method": "POST",
         "path": "/api/users", "status_code": 201, "at": rows[2]["at"]},
    ]
    assert client.get("/api/audit?username=nobody", headers=admin).json() == []


# ---------------------------------------------------------------- /audit/verify


def test_链校验_非空分支精确形状与键序(client, admin, seeded):
    body = client.get("/api/audit/verify", headers=admin).json()
    assert list(body.keys()) == VERIFY_FULL_KEYS
    first_id, last_id = body["from_id"], body["to_id"]
    assert body == {
        "checked": 3,
        "legacy_unchained": 0,
        "from_id": first_id,
        "to_id": last_id,
        "partial_segment": False,
        "valid": True,
        "broken_at": None,   # 值可空的恒在键：链完好时为 null，键不消失
        "reason": "",
        "caliber": CALIBER,
    }
    # 起点不是链首：只能证明段内自洽，partial_segment 如实报 True
    partial = client.get(f"/api/audit/verify?start_id={first_id + 1}", headers=admin).json()
    assert partial == {
        "checked": 2,
        "legacy_unchained": 0,
        "from_id": first_id + 1,
        "to_id": last_id,
        "partial_segment": True,
        "valid": True,
        "broken_at": None,
        "reason": "",
        "caliber": CALIBER,
    }


def test_链校验_空区间分支精确(client, admin, seeded):
    body = client.get("/api/audit/verify?start_id=999999", headers=admin).json()
    # 空分支只有三个键——note 不得在非空分支出现，非空九键也不得漏进空分支
    assert list(body.keys()) == ["checked", "valid", "note"]
    assert body == {"checked": 0, "valid": True, "note": "该区间没有审计记录"}


def test_链校验_锚点对账三分支精确(client, admin, seeded):
    with SessionLocal() as db:
        row = db.query(AuditLog).order_by(AuditLog.id).first()
        anchor_id, anchor_hash = row.id, row.entry_hash
    ok = client.get(
        f"/api/audit/verify?anchor_id={anchor_id}&anchor_hash={anchor_hash}", headers=admin
    ).json()
    assert list(ok.keys()) == VERIFY_FULL_KEYS + ANCHOR_KEYS
    assert ok == {
        "checked": 3,
        "legacy_unchained": 0,
        "from_id": anchor_id,
        "to_id": ok["to_id"],
        "partial_segment": False,
        "valid": True,
        "broken_at": None,
        "reason": "",
        "caliber": CALIBER,
        "anchor_id": anchor_id,
        "anchor_match": True,
        "anchor_reason": "",
    }
    mismatch = client.get(
        f"/api/audit/verify?anchor_id={anchor_id}&anchor_hash=ffff", headers=admin
    ).json()
    assert mismatch["valid"] is False and mismatch["anchor_match"] is False
    assert mismatch["anchor_reason"] == "该行 entry_hash 与外部锚点不符——锚点时刻之后该行被改动"
    # 锚点所指行不在库中：空分支 + 锚点键，六键按出键序
    gone = client.get(
        "/api/audit/verify?anchor_id=999999&anchor_hash=ffff", headers=admin
    ).json()
    assert list(gone.keys()) == ["checked", "valid", "note"] + ANCHOR_KEYS
    assert gone == {
        "checked": 0,
        "valid": False,
        "note": "该区间没有审计记录",
        "anchor_id": 999999,
        "anchor_match": False,
        "anchor_reason": "锚点所指的行已不在库中——疑似末尾截断"
                         "（若该段已归档，请核对归档 manifest 后续查）",
    }
    assert client.get("/api/audit/verify?anchor_id=1", headers=admin).status_code == 422


def test_链校验_篡改后broken_at是断点id(client, admin, seeded):
    """直接改库里一条历史记录——broken_at 恰在被改的那条（int，不再是 null）。"""
    with SessionLocal() as db:
        victim = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        victim_id = victim.id
        original = victim.path
        victim.path = "/api/tampered"
        db.commit()
    try:
        body = client.get("/api/audit/verify", headers=admin).json()
        assert list(body.keys()) == VERIFY_FULL_KEYS
        assert body["valid"] is False
        assert body["broken_at"] == victim_id and type(body["broken_at"]) is int
        assert body["reason"] == "本条内容与哈希不符"
    finally:
        with SessionLocal() as db:
            row = db.get(AuditLog, victim_id)
            row.path = original
            db.commit()
    assert client.get("/api/audit/verify", headers=admin).json()["valid"] is True


# ---------------------------------------------------------------- /audit/stats


def test_审计统计精确形状与键序(client, admin, seeded):
    uid = seeded["user"]["id"]
    body = client.get("/api/audit/stats?days=7", headers=admin).json()
    assert list(body.keys()) == STATS_KEY_ORDER
    today = client.get("/api/audit?limit=1", headers=admin).json()[0]["at"][:10]
    assert body == {
        "days": 7,
        "scope": "全部实例（审计落库，非进程内计数）",
        "total": 3,
        "failed": 1,
        "failed_ratio_pct": 33.33,
        "daily": [{"date": today, "ok": 2, "failed": 1}],
        "failed_status_codes": [{"status": 409, "count": 1}],
        "top_users": [{"key": "admin", "count": 3}],
        # /api/users 计 2 次（201+409）> role 1 次，排序无并列歧义
        "top_paths": [{"key": "/api/users", "count": 2},
                      {"key": f"/api/users/{uid}/role", "count": 1}],
        "top_failed_paths": [{"key": "/api/users", "count": 1}],
    }
    # failed_ratio_pct 恒 float：round(x*100, 2) 与空表兜底 0.0 都是 float
    assert isinstance(body["failed_ratio_pct"], float)
    assert client.get("/api/audit/stats?days=0", headers=admin).json()["days"] == 1
    assert client.get("/api/audit/stats?days=99999", headers=admin).json()["days"] == 365


# ------------------------------------------------------------ /users/role-changes


def test_角色变更记录精确形状与键序(client, admin, seeded):
    rows = client.get("/api/users/role-changes", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [ROLE_CHANGE_KEYS]
    uid = seeded["user"]["id"]
    assert isinstance(rows[0]["at"], str) and "T" in rows[0]["at"]
    assert rows == [{
        "id": rows[0]["id"],
        "user_id": uid,
        "old_role": "doctor",
        "new_role": "operator",
        "changed_by": 1,
        "at": rows[0]["at"],
    }]
    assert client.get(f"/api/users/role-changes?user_id={uid}", headers=admin).json() == rows
    assert client.get("/api/users/role-changes?user_id=999999", headers=admin).json() == []


# ---------------------------------------------------------------- /audit/export


def test_归档导出_NDJSON逐行精确(client, admin, seeded):
    resp = client.get("/api/audit/export", headers=admin)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-ndjson"
    assert resp.headers["content-disposition"] == 'attachment; filename="audit_logs.ndjson"'
    lines = [json.loads(line) for line in resp.text.strip().split("\n")]
    audit_rows = {r["id"]: r for r in client.get("/api/audit", headers=admin).json()}
    assert len(lines) == 4  # 3 条记录 + 1 条 meta
    for row in lines[:3]:
        assert list(row.keys()) == [
            "id", "user_id", "username", "method", "path", "status_code", "at",
        ]
        mirror = audit_rows[row["id"]]
        assert row == {
            "id": row["id"], "user_id": 1, "username": "admin",
            "method": mirror["method"], "path": mirror["path"],
            "status_code": mirror["status_code"], "at": mirror["at"],
        }
    last_id = lines[2]["id"]
    assert lines[3] == {"_meta": True, "total": 3, "since_id": 0, "last_id": last_id}
    # 增量导出：since_id 之后只剩 meta 行
    tail = client.get(f"/api/audit/export?since_id={last_id}", headers=admin)
    assert [json.loads(x) for x in tail.text.strip().split("\n")] == [
        {"_meta": True, "total": 0, "since_id": last_id, "last_id": last_id}
    ]
    assert client.get("/api/audit/export?until=bad", headers=admin).status_code == 422
