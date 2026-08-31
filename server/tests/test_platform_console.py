"""阶段二 平台管控台：运行监控、审计统计、价格公示、就诊凭据。

这四块的共同点是"给人做判断用的"，所以用例盯的是**会不会误导判断**：
监控数据是进程内的会不会被当成全集群、停用项目会不会挂在公示页上、
作废的卡查询时是"查无此卡"还是"这张卡废了"。
"""
import pytest


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "管控台县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


def _staff(client, admin, org, username, role):
    client.post(
        "/api/users",
        json={"username": username, "password": "passw0rd1", "role": role, "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def operator(client, admin, org):
    return _staff(client, admin, org, "pc_op", "operator")


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients", json={"name": "凭据患者", "id_card": "331782199003031234"}, headers=admin
    ).json()


# ---------------- 运行监控 ----------------


def test_概览标明数据是本实例而非全集群(client, admin):
    """多实例部署时把单机数据当全局看，扩容后会误判流量掉了一半。"""
    data = client.get("/api/monitor/overview", headers=admin).json()
    assert "本实例" in data["scope"]
    assert data["instance_id"]
    assert data["database"]["connected"] is True
    assert data["uptime_seconds"] >= 0


def test_未配置redis时明说无从得知节点数(client, admin):
    """不拿"本实例"冒充"全集群"——单机部署与漏配 Redis 的处置完全不同。"""
    data = client.get("/api/monitor/nodes", headers=admin).json()
    assert data["scope"] == "unknown"
    assert data["instances"] is None
    assert "Redis" in data["note"]


def test_未配置redis在概览里是提示而非故障(client, admin):
    data = client.get("/api/monitor/overview", headers=admin).json()["redis"]
    assert data["configured"] is False
    assert "多实例" in data["note"]


def test_调用统计按模块聚合并保留错误样本(client, admin):
    client.get("/api/patients?keyword=不存在的人", headers=admin)
    client.get("/api/organizations/999999", headers=admin)   # 4xx，进错误样本
    data = client.get("/api/monitor/api-stats", headers=admin).json()
    assert data["total_requests"] > 0
    assert "进程重启即清零" in data["scope"]
    modules = {m["module"] for m in data["top_modules"]}
    assert "patients" in modules
    assert any(e["status"] >= 400 for e in data["error_samples"])


def test_监控台限管理员(client, operator):
    for path in ("/api/monitor/overview", "/api/monitor/api-stats", "/api/monitor/nodes"):
        assert client.get(path, headers=operator).status_code == 403


# ---------------- 审计统计 ----------------


def test_审计统计区分成功与失败(client, admin, org):
    """只看总量看不出"改坏了多少次"。"""
    client.post("/api/organizations", json={"name": "x", "org_type": "township",
                                            "level": "township"}, headers=admin)  # 422
    data = client.get("/api/audit/stats?days=30", headers=admin).json()
    assert data["total"] > 0
    assert data["daily"] and {"date", "ok", "failed"} <= set(data["daily"][0])
    assert data["failed"] >= 1
    assert data["failed_status_codes"]
    assert any(u["key"] == "admin" for u in data["top_users"])


def test_审计统计标明是落库数据不是进程内计数(client, admin):
    """与 /api/monitor/api-stats 是两个视角，不能互相替代。"""
    data = client.get("/api/audit/stats", headers=admin).json()
    assert "审计落库" in data["scope"]


def test_审计统计天数被限幅(client, admin):
    assert client.get("/api/audit/stats?days=99999", headers=admin).json()["days"] == 365
    assert client.get("/api/audit/stats?days=0", headers=admin).json()["days"] == 1


# ---------------- 价格管理与公示 ----------------


@pytest.fixture(scope="module")
def charge_item(client, admin):
    return client.post(
        "/api/billing/charge-items",
        json={"code": "PC-CT", "name": "头颅CT平扫", "category": "exam", "price": 260},
        headers=admin,
    ).json()


def test_调价留下依据与生效日期(client, admin, charge_item):
    resp = client.post(
        f"/api/billing/charge-items/{charge_item['id']}/reprice",
        json={"new_price": 220, "reason": "省医保局2026年第3号文", "effective_date": "2026-09-01"},
        headers=admin,
    )
    assert resp.status_code == 200 and resp.json()["price"] == 220
    history = client.get(
        f"/api/billing/charge-items/{charge_item['id']}/price-history", headers=admin
    ).json()
    assert history[0]["old_price"] == 260 and history[0]["new_price"] == 220
    assert history[0]["reason"] == "省医保局2026年第3号文"
    assert history[0]["effective_date"] == "2026-09-01"


def test_同价调价被拒(client, admin, charge_item):
    resp = client.post(
        f"/api/billing/charge-items/{charge_item['id']}/reprice",
        json={"new_price": 220}, headers=admin,
    )
    assert resp.status_code == 409


def test_走patch改价也会留痕(client, admin):
    """不能靠调用方自觉走 reprice——留痕要在唯一的写入路径上兜住。"""
    item = client.post(
        "/api/billing/charge-items",
        json={"code": "PC-MRI", "name": "头颅MRI", "category": "exam", "price": 600},
        headers=admin,
    ).json()
    client.patch(f"/api/billing/charge-items/{item['id']}", json={"price": 560}, headers=admin)
    history = client.get(
        f"/api/billing/charge-items/{item['id']}/price-history", headers=admin
    ).json()
    assert len(history) == 1 and history[0]["new_price"] == 560


def test_改名不产生调价记录(client, admin):
    item = client.post(
        "/api/billing/charge-items",
        json={"code": "PC-US", "name": "腹部B超", "category": "exam", "price": 80},
        headers=admin,
    ).json()
    client.patch(f"/api/billing/charge-items/{item['id']}", json={"name": "腹部超声"}, headers=admin)
    assert client.get(
        f"/api/billing/charge-items/{item['id']}/price-history", headers=admin
    ).json() == []


def test_价格公示无需登录且带最近调价时间(client, charge_item):
    """公示是给不特定公众看的，加登录背离目的。"""
    rows = client.get("/api/portal/price-list").json()
    row = next(r for r in rows if r["code"] == "PC-CT")
    assert row["price"] == 220
    assert row["category_name"] == "检查检验"     # 不把 exam 这种枚举抛给患者
    assert row["effective_date"] == "2026-09-01"
    assert row["last_adjusted_at"]


def test_停用项目不上公示页(client, admin):
    """患者按公示价来问价却收不到这个费，等于自找纠纷。"""
    item = client.post(
        "/api/billing/charge-items",
        json={"code": "PC-OLD", "name": "已停用项目", "category": "other", "price": 30},
        headers=admin,
    ).json()
    assert any(r["code"] == "PC-OLD" for r in client.get("/api/portal/price-list").json())
    client.patch(f"/api/billing/charge-items/{item['id']}", json={"active": False}, headers=admin)
    assert not any(r["code"] == "PC-OLD" for r in client.get("/api/portal/price-list").json())


# ---------------- 就诊凭据 ----------------


def test_发放凭据自动作废旧卡(client, operator, patient):
    """挂失换发时旧卡必须当场失效，否则捡到卡的人还能拿它挂号。"""
    first = client.post(
        "/api/credentials", json={"patient_id": patient["id"], "credential_type": "card"},
        headers=operator,
    ).json()
    assert first["status"] == "active"
    second = client.post(
        "/api/credentials",
        json={"patient_id": patient["id"], "credential_type": "card", "reason": "原卡遗失"},
        headers=operator,
    ).json()
    assert second["superseded"] == [first["credential_no"]]
    old = client.get(f"/api/credentials/lookup/{first['credential_no']}", headers=operator).json()
    assert old["status"] == "void" and old["valid"] is False
    assert old["close_reason"] == "原卡遗失"


def test_失效凭据核验返回状态而非404(client, operator, patient):
    """窗口人员要知道"这张卡作废了"，不是"查无此卡"——处置完全不同。"""
    creds = client.get(
        f"/api/credentials?patient_id={patient['id']}&status=void", headers=operator
    ).json()
    resp = client.get(f"/api/credentials/lookup/{creds[0]['credential_no']}", headers=operator)
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["patient"]["name"] == "凭据患者"


def test_凭据号不存在才是404(client, operator):
    assert client.get("/api/credentials/lookup/NO-SUCH-CARD", headers=operator).status_code == 404


def test_回收与作废分开记(client, operator, admin):
    """回收是正常结束，作废是异常终止；统计报损率时必须区分。"""
    p = client.post(
        "/api/patients", json={"name": "回收患者", "id_card": "331782199003031299"}, headers=admin
    ).json()
    cred = client.post("/api/credentials", json={"patient_id": p["id"]}, headers=operator).json()
    resp = client.post(f"/api/credentials/{cred['id']}/recycle", json={}, headers=operator)
    assert resp.status_code == 200
    assert resp.json()["status"] == "recycled"
    assert resp.json()["close_reason"] == "患者交回"
    # 已结束的凭据不可再操作
    assert client.post(
        f"/api/credentials/{cred['id']}/void", json={"reason": "重复操作"}, headers=operator
    ).status_code == 409


def test_作废必须填原因(client, operator, admin):
    p = client.post(
        "/api/patients", json={"name": "作废患者", "id_card": "331782199003031288"}, headers=admin
    ).json()
    cred = client.post("/api/credentials", json={"patient_id": p["id"]}, headers=operator).json()
    assert client.post(
        f"/api/credentials/{cred['id']}/void", json={"reason": ""}, headers=operator
    ).status_code == 422


def test_凭据号重复被拒(client, operator, patient):
    body = {"patient_id": patient["id"], "credential_no": "FIXED-CARD-001"}
    assert client.post("/api/credentials", json=body, headers=operator).status_code == 201
    assert client.post("/api/credentials", json=body, headers=operator).status_code == 409


def test_凭据不改变患者身份标识(client, operator, admin, patient):
    """卡是介质，健康卡号是身份。换卡不该动 ehc_no，否则历史档案跟着断。"""
    before = patient["ehc_no"]
    client.post("/api/credentials", json={"patient_id": patient["id"]}, headers=operator)
    after = client.get(f"/api/patients/{before}", headers=admin).json()["ehc_no"]
    assert before == after
