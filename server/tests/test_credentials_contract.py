"""就诊凭据 `/api/credentials` 八个端点的**特征化网 + 响应契约**。

套路同 `test_rules_contract.py` / `test_dataquality_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

建模判断：

- 发放回执 = 列表行 + `superseded`（挂失换发时被作废的旧凭据号列表，首发为空
  列表**恒在**）；核验回执 = 列表行 + `valid` + `patient`。键集合按端点固定，
  按「键集合不同就两个模型」派生三个模型，不用 exclude_unset。
- `GET /resolve` 的 `credential_status` 是**条件键**：只在命中实体凭据号那一支
  出现，健康卡号/身份证分支**整个不在**（不是 null）——两支都钉，端点
  `response_model_exclude_unset=True`。
- `issued_at`/`closed_at` 是 DateTime 列的 `.isoformat()` 字符串（后者可 null）；
  一码通的 `code`/`remaining_seconds` 含随机与时间量，钉形状与边界不钉值
  （docs/接口标准与治理.md「随机项不能进比对」）。本模块无 Money/Float 列。
"""
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
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def patients(client, admin):
    first = client.post(
        "/api/patients", json={"name": "凭据契约患者一", "id_card": "330281199203046014"},
        headers=admin,
    )
    assert first.status_code in (200, 201), first.text
    second = client.post(
        "/api/patients", json={"name": "凭据契约患者二", "id_card": "320000199501011234"},
        headers=admin,
    )
    assert second.status_code in (200, 201), second.text
    return {"first": first.json(), "second": second.json()}


ISSUE_KEY_ORDER = [
    "id", "patient_id", "credential_no", "credential_type", "credential_type_name",
    "status", "status_name", "issued_at", "closed_at", "close_reason", "superseded",
]
ROW_KEY_ORDER = ISSUE_KEY_ORDER[:-1]


@pytest.fixture(scope="module")
def issued(client, admin, patients):
    """患者一：先发实体卡，再发二维码（换发语义：旧卡自动作废）。"""
    card = client.post(
        "/api/credentials",
        json={"patient_id": patients["first"]["id"], "credential_type": "card",
              "credential_no": "CT-CARD-001"},
        headers=admin,
    )
    assert card.status_code == 201, card.text
    qrcode = client.post(
        "/api/credentials",
        json={"patient_id": patients["first"]["id"], "credential_type": "qrcode"},
        headers=admin,
    )
    assert qrcode.status_code == 201, qrcode.text
    return {"card": card.json(), "qrcode": qrcode.json()}


def test_发放回执精确形状与键序(issued, patients):
    body = issued["card"]
    assert list(body.keys()) == ISSUE_KEY_ORDER
    assert "T" in body["issued_at"]  # DateTime 列 isoformat 字符串出参
    assert body == {
        "id": body["id"],
        "patient_id": patients["first"]["id"],
        "credential_no": "CT-CARD-001",
        "credential_type": "card",
        "credential_type_name": "实体就诊卡",
        "status": "active",
        "status_name": "有效",
        "issued_at": body["issued_at"],
        "closed_at": None,
        "close_reason": "",
        "superseded": [],
    }


def test_换发回执_旧卡号列入superseded(issued, patients):
    body = issued["qrcode"]
    # 系统生成凭据号：健康卡号-类型首字母+序号（此人第 2 张 → Q02）
    assert body == {
        "id": body["id"],
        "patient_id": patients["first"]["id"],
        "credential_no": f"{patients['first']['ehc_no']}-Q02",
        "credential_type": "qrcode",
        "credential_type_name": "电子二维码",
        "status": "active",
        "status_name": "有效",
        "issued_at": body["issued_at"],
        "closed_at": None,
        "close_reason": "",
        "superseded": ["CT-CARD-001"],
    }


def test_列表行与回执同形_无superseded键(client, admin, issued, patients):
    resp = client.get(
        f"/api/credentials?patient_id={patients['first']['id']}", headers=admin
    )
    rows = resp.json()
    assert resp.headers["X-Total-Count"] == "2"
    assert [list(r.keys()) for r in rows] == [ROW_KEY_ORDER, ROW_KEY_ORDER]
    qr_row = {k: v for k, v in issued["qrcode"].items() if k != "superseded"}
    # 旧卡被自动作废：closed_at 落时刻、理由为缺省的「换发新凭据」
    card_row = {
        **{k: v for k, v in issued["card"].items() if k != "superseded"},
        "status": "void", "status_name": "已作废",
        "closed_at": rows[1]["closed_at"], "close_reason": "换发新凭据",
    }
    assert "T" in rows[1]["closed_at"]
    assert rows == [qr_row, card_row]  # id 倒序
    assert client.get(
        f"/api/credentials?patient_id={patients['first']['id']}&status=active", headers=admin
    ).json() == [qr_row]


def test_核验回执精确形状与键序(client, admin, issued, patients):
    qr_no = issued["qrcode"]["credential_no"]
    body = client.get(f"/api/credentials/lookup/{qr_no}", headers=admin).json()
    assert list(body.keys()) == ROW_KEY_ORDER + ["valid", "patient"]
    assert list(body["patient"].keys()) == ["id", "name", "ehc_no"]
    assert body == {
        **{k: v for k, v in issued["qrcode"].items() if k != "superseded"},
        "valid": True,
        "patient": {"id": patients["first"]["id"], "name": "凭据契约患者一",
                    "ehc_no": patients["first"]["ehc_no"]},
    }
    # 失效凭据不按 404 处理：附状态返回（窗口要知道"这张卡作废了"）
    voided = client.get("/api/credentials/lookup/CT-CARD-001", headers=admin).json()
    assert voided["valid"] is False
    assert voided["status"] == "void" and voided["status_name"] == "已作废"


def test_回收与作废回执精确(client, admin, patients):
    temp = client.post(
        "/api/credentials",
        json={"patient_id": patients["second"]["id"], "credential_type": "temp"},
        headers=admin,
    ).json()
    recycled = client.post(
        f"/api/credentials/{temp['id']}/recycle", json={}, headers=admin
    ).json()
    # 回收/作废回执 = 列表行形状（没有 superseded 键）
    assert list(recycled.keys()) == ROW_KEY_ORDER
    assert recycled == {
        **{k: v for k, v in temp.items() if k != "superseded"},
        "status": "recycled", "status_name": "已回收",
        "closed_at": recycled["closed_at"], "close_reason": "患者交回",
    }
    assert "T" in recycled["closed_at"]

    card = client.post(
        "/api/credentials",
        json={"patient_id": patients["second"]["id"], "credential_type": "card",
              "credential_no": "CT-CARD-P2"},
        headers=admin,
    ).json()
    assert card["superseded"] == []  # 已回收的不在换发作废之列
    assert client.post(
        f"/api/credentials/{card['id']}/void", json={}, headers=admin
    ).status_code == 422  # 作废必须给原因
    voided = client.post(
        f"/api/credentials/{card['id']}/void", json={"reason": "卡片损坏"}, headers=admin
    ).json()
    assert voided == {
        **{k: v for k, v in card.items() if k != "superseded"},
        "status": "void", "status_name": "已作废",
        "closed_at": voided["closed_at"], "close_reason": "卡片损坏",
    }
    # 已关闭的凭据不可再操作
    assert client.post(
        f"/api/credentials/{card['id']}/recycle", json={}, headers=admin
    ).status_code == 409


ONE_CODE_NOTE = "动态码，过期即失效；不落库，换平台密钥即全部作废"


def test_一码通签发与核验精确形状与键序(client, admin, patients):
    body = client.post(
        "/api/credentials/one-code",
        json={"patient_id": patients["first"]["id"], "ttl_seconds": 60},
        headers=admin,
    ).json()
    assert list(body.keys()) == ["code", "ehc_no", "expires_in", "note"]
    # code 是自包含串「健康卡号.过期时刻.签名」，含时间量——钉形状不钉值
    assert body == {
        "code": body["code"],
        "ehc_no": patients["first"]["ehc_no"],
        "expires_in": 60,
        "note": ONE_CODE_NOTE,
    }
    parts = body["code"].split(".")
    assert len(parts) == 3 and parts[0] == patients["first"]["ehc_no"]

    resolved = client.post(
        "/api/credentials/one-code/resolve", json={"code": body["code"]}, headers=admin
    ).json()
    assert list(resolved.keys()) == ["patient_id", "name", "ehc_no", "remaining_seconds"]
    assert resolved == {
        "patient_id": patients["first"]["id"],
        "name": "凭据契约患者一",
        "ehc_no": patients["first"]["ehc_no"],
        "remaining_seconds": resolved["remaining_seconds"],
    }
    assert isinstance(resolved["remaining_seconds"], int)
    assert 0 < resolved["remaining_seconds"] <= 60
    # 伪造签名走 403（与过期 410 分开报）
    forged = f"{parts[0]}.{parts[1]}.{'0' * 32}"
    assert client.post(
        "/api/credentials/one-code/resolve", json={"code": forged}, headers=admin
    ).status_code == 403


def test_多卡协同三分支精确(client, admin, issued, patients):
    brief_one = {"id": patients["first"]["id"], "name": "凭据契约患者一",
                 "ehc_no": patients["first"]["ehc_no"]}
    # 分支一：命中实体凭据号——带 credential_status
    by_no = client.get(
        "/api/credentials/resolve",
        params={"identifier": issued["qrcode"]["credential_no"]},
        headers=admin,
    ).json()
    assert list(by_no.keys()) == ["matched_by", "credential_status", "valid", "patient"]
    assert by_no == {
        "matched_by": "credential_no",
        "credential_status": "active",
        "valid": True,
        "patient": brief_one,
    }
    # 作废凭据照样命中：状态如实、valid False
    by_void = client.get(
        "/api/credentials/resolve", params={"identifier": "CT-CARD-001"}, headers=admin
    ).json()
    assert by_void == {
        "matched_by": "credential_no",
        "credential_status": "void",
        "valid": False,
        "patient": brief_one,
    }
    # 分支二：命中健康卡号——credential_status 键**整个不在**（不是 null）
    by_ehc = client.get(
        "/api/credentials/resolve",
        params={"identifier": patients["first"]["ehc_no"]},
        headers=admin,
    ).json()
    assert list(by_ehc.keys()) == ["matched_by", "valid", "patient"]
    assert "credential_status" not in by_ehc
    assert by_ehc == {"matched_by": "ehc_no", "valid": True, "patient": brief_one}
    # 分支三：命中身份证号
    by_id_card = client.get(
        "/api/credentials/resolve", params={"identifier": "320000199501011234"}, headers=admin
    ).json()
    assert by_id_card == {
        "matched_by": "id_card",
        "valid": True,
        "patient": {"id": patients["second"]["id"], "name": "凭据契约患者二",
                    "ehc_no": patients["second"]["ehc_no"]},
    }
    assert client.get(
        "/api/credentials/resolve", params={"identifier": "谁也不是"}, headers=admin
    ).status_code == 404
