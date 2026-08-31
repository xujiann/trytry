"""慢专病子系统与平台的集成契约：领域事件、订阅者、数据源采集、定时任务。

这套用例守的是**集成方式**：出院之后随访计划是不是自己出来了、订阅者炸了会不会
把出院办理带崩、子系统关掉之后平台还能不能正常跑。
"""
from itertools import count

import pytest

from app import events

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "集成契约测试院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "事件患者", "id_card": "330777199003030001", "gender": "男",
              "birth_date": "1990-03-03"},
        headers=h,
    ).json()
    return {"org": org, "patient": patient}


# ============================================================ 事件总线本身


def test_未知事件名不得订阅():
    """拼错的事件名不会报错、只会静默地没人听——所以在订阅时就拒绝。"""
    with pytest.raises(ValueError):
        events.subscribe("addmission.discharged", lambda db, payload: None)


def test_订阅者异常不影响发布方(client, h, base):
    """契约 2：一个订阅者炸掉不能连累业务主流程。"""

    def boom(db, payload):
        raise RuntimeError("订阅者故意炸")

    events.subscribe(events.ENCOUNTER_CREATED, boom)
    try:
        resp = client.post(
            "/api/encounters",
            json={"patient_id": base["patient"]["id"], "org_id": base["org"]["id"],
                  "encounter_type": "outpatient", "diagnosis_code": "J06",
                  "diagnosis_name": "急性上呼吸道感染"},
            headers=h,
        )
        assert resp.status_code == 201, "订阅者异常不该让就诊登记失败"
    finally:
        events._subscribers[events.ENCOUNTER_CREATED].remove(boom)


def test_子系统订阅已装上():
    assert events.subscriber_count(events.ADMISSION_DISCHARGED) >= 1
    assert events.subscriber_count(events.ENCOUNTER_CREATED) >= 1


def test_重复注册订阅不会重复投递():
    from app.spd.subscribers import register_subscribers

    before = events.subscriber_count(events.ADMISSION_DISCHARGED)
    register_subscribers()
    assert events.subscriber_count(events.ADMISSION_DISCHARGED) == before


# ============================================================ 出院 → 随访计划


_seq = count(1)


def _discharge(client, h, base, diagnosis: str):
    """建一次完整住院并出院，返回出院响应。出院要过病案首页与费用两道校验。

    病区/床位每次新建（名称带序号）：同名病区会 409，而本文件要**重复出院**
    来验证订阅者的幂等性。
    """
    seq = next(_seq)
    ward = client.post(
        "/api/inpatient/wards",
        json={"org_id": base["org"]["id"], "name": f"内科病区{seq}"},
        headers=h,
    ).json()
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["id"], "bed_no": f"B{seq:03d}"},
        headers=h,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": base["patient"]["id"], "ward_id": ward["id"],
              "bed_id": bed["id"], "doctor_name": "张医生", "diagnosis_name": diagnosis},
        headers=h,
    ).json()
    client.post(
        f"/api/inpatient/admissions/{admission['id']}/case-summary",
        json={"discharge_diagnosis": diagnosis, "outcome": "好转"},
        headers=h,
    )
    return client.post(
        f"/api/inpatient/admissions/{admission['id']}/discharge", headers=h
    )


def test_未配关键词的方案不会被出院事件命中(client, h, base):
    """种子方案没有诊断关键词——不匹配任何人，与 auto-match 同一口径。"""
    before = client.get(
        f"/api/spd/followup-records?patient_id={base['patient']['id']}", headers=h
    ).json()
    resp = _discharge(client, h, base, "普通肺炎")
    assert resp.status_code == 200, resp.text
    after = client.get(
        f"/api/spd/followup-records?patient_id={base['patient']['id']}", headers=h
    ).json()
    assert len(after) == len(before), "没配关键词的方案不该命中任何人"


def test_出院事件按关键词自动生成随访计划(client, h, base):
    rules = client.get("/api/spd/followup-rules?scene=inpatient", headers=h).json()
    rule = next(r for r in rules if r["code"] == "fr_discharge")
    client.patch(
        f"/api/spd/followup-rules/{rule['id']}",
        json={"diagnosis_keywords": ["高血压"]}, headers=h,
    )
    resp = _discharge(client, h, base, "高血压3级")
    assert resp.status_code == 200, resp.text

    records = client.get(
        f"/api/spd/followup-records?patient_id={base['patient']['id']}", headers=h
    ).json()
    generated = [r for r in records if r["rule_id"] == rule["id"]]
    assert len(generated) == len(rule["points"]), "应按方案的每个时间点各生成一条"
    assert {r["status"] for r in generated} == {"planned"}


def test_出院事件是幂等的(client, h, base):
    """同一患者同一方案只派生一次——重复投递不该变成两份随访计划。"""
    rules = client.get("/api/spd/followup-rules?scene=inpatient", headers=h).json()
    assert any(r["code"] == "fr_discharge" for r in rules)  # 出院随访规则须存在
    before = len(client.get(
        f"/api/spd/followup-records?patient_id={base['patient']['id']}", headers=h
    ).json())
    resp = _discharge(client, h, base, "高血压3级")
    assert resp.status_code == 200
    after = len(client.get(
        f"/api/spd/followup-records?patient_id={base['patient']['id']}", headers=h
    ).json())
    assert after == before, f"重复出院不该再派生（{before} → {after}）"


# ============================================================ 就诊 → 疑似识别（默认关）


def test_就诊自动识别默认关闭(client, h, base):
    from app.config import settings

    assert settings.spd_auto_identify_on_encounter is False, (
        "默认必须关闭：全域自动识别会在生产上产生大量疑似记录"
    )


def test_开启后就诊事件识别疑似人群(client, h, base, monkeypatch):
    from app.config import settings

    patient = client.post(
        "/api/patients",
        json={"name": "事件识别患者", "id_card": "330777199003030002", "gender": "女",
              "birth_date": "1975-05-05"},
        headers=h,
    ).json()
    monkeypatch.setattr(settings, "spd_auto_identify_on_encounter", True)
    resp = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": base["org"]["id"],
              "encounter_type": "outpatient", "diagnosis_code": "I10",
              "diagnosis_name": "原发性高血压"},
        headers=h,
    )
    assert resp.status_code == 201

    candidates = client.get(
        "/api/spd/candidates?program_code=hypertension", headers=h
    ).json()
    hit = [c for c in candidates if c["patient_id"] == patient["id"]]
    assert hit, "命中纳入规则的就诊应产生疑似记录"
    assert hit[0]["status"] == "suspect", "只写疑似，不写纳管——筛出来 ≠ 管起来"
    assert hit[0]["source"] == "event"

    enrollments = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()
    assert not [e for e in enrollments if e["patient_id"] == patient["id"]]


def test_就诊识别是幂等的(client, h, base, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "spd_auto_identify_on_encounter", True)
    patient_id = client.get(
        "/api/spd/candidates?program_code=hypertension", headers=h
    ).json()[0]["patient_id"]
    before = len(client.get(
        f"/api/spd/screenings?patient_id={patient_id}", headers=h
    ).json())
    client.post(
        "/api/encounters",
        json={"patient_id": patient_id, "org_id": base["org"]["id"],
              "encounter_type": "outpatient", "diagnosis_code": "I10",
              "diagnosis_name": "原发性高血压"},
        headers=h,
    )
    after = len(client.get(
        f"/api/spd/screenings?patient_id={patient_id}", headers=h
    ).json())
    assert after == before, "已在池中的患者不该被重复识别"


# ============================================================ 数据源采集


def test_采集器跑通并写同步日志(client, h, base):
    """用**真会落库**的 publichealth 采集器验这条链路。

    原先这里用的是 HIS——那时 HIS 挂着一个只 `count()` 不写库的探针，
    这条用例于是在证明"假采集器也能报成功"。探针摘掉注册后（P1-2），
    HIS 如实变成"未注册"，本用例改用唯一一个真实现。
    """
    from app.database import SessionLocal
    from app.spd.collectors import run_due_sources

    source = client.post(
        "/api/spd/data-sources",
        json={"code": "ph_main", "name": "公卫随访库", "source_type": "publichealth",
              "org_id": base["org"]["id"], "freq_minutes": 5},
        headers=h,
    ).json()
    assert source["status"] == "running"

    with SessionLocal() as db:
        count, summary = run_due_sources(db)
        db.commit()
    assert count >= 1, summary

    logs = client.get(f"/api/spd/data-sources/{source['id']}/sync-logs", headers=h).json()
    assert logs and logs[0]["success"] is True
    refreshed = [
        s for s in client.get("/api/spd/data-sources", headers=h).json()
        if s["id"] == source["id"]
    ][0]
    assert refreshed["last_sync_at"], "跑完要回填最近同步时间"
    assert refreshed["success_rate"] == 100.0


def test_未注册类型的数据源记为失败而不是静默(client, h, base):
    from app.database import SessionLocal
    from app.spd.collectors import run_due_sources, unregistered_types

    source = client.post(
        "/api/spd/data-sources",
        json={"code": "lis_main", "name": "县医院LIS", "source_type": "LIS",
              "freq_minutes": 5},
        headers=h,
    ).json()
    with SessionLocal() as db:
        assert "LIS" in unregistered_types(db)
        run_due_sources(db)
        db.commit()

    logs = client.get(f"/api/spd/data-sources/{source['id']}/sync-logs", headers=h).json()
    assert logs[0]["success"] is False
    assert "未注册" in logs[0]["message"]
    monitor = client.get("/api/spd/data-sources-monitor", headers=h).json()
    assert monitor["by_status"].get("failed", 0) >= 1, "失败要在监控页上看得见"


def test_未到期的数据源不重复跑(client, h):
    from app.database import SessionLocal
    from app.spd.collectors import run_due_sources

    with SessionLocal() as db:
        count, summary = run_due_sources(db)
        db.commit()
    assert count == 0, f"刚跑过的源不该再跑：{summary}"


# ============================================================ 定时任务


def test_子系统定时任务已注册():
    from app.scheduler import REGISTRY

    assert {"spd_data_source_sync", "spd_task_overdue_scan"} <= set(REGISTRY)


def test_超期扫描任务与工作台同口径(client, h, base):
    from app.database import SessionLocal
    from app.spd.jobs import spd_task_overdue_scan

    client.post(
        "/api/spd/tasks",
        json={"patient_id": base["patient"]["id"], "title": "昨天就该办的",
              "task_type": "followup", "due_days": 0},
        headers=h,
    )
    with SessionLocal() as db:
        count, summary = spd_task_overdue_scan(db)
        db.commit()
    assert isinstance(count, int) and "超期" in summary
