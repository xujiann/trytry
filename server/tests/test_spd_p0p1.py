"""P0/P1 补强项的契约用例：报告推送、三类超期、宣教外发、公卫采集、迁入建档、进入条件。

这些用例守的都是**口径**而不是接口形状：
- 报告推送的幂等键是 (task, period_label)，不是"现在是不是整点"；
- 三类超期都落 status，失访不会被覆盖成超期；
- 宣教"发出去"必须真走通道，失败置 failed 而不是假装 sent；
- 公卫采集用 source_ref 判重，补跑不产生重复行；
- 迁入确认要在目标机构**重建**责任关系，且排除后允许再次纳管；
- 路径进入条件不满足 → 暂停 + 通知，条件满足后可恢复。
"""
from datetime import date, timedelta

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


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "P0P1测试卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "P0P1迁入卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"补强患者{i}", "id_card": f"33052419750505{i:04d}",
                  "gender": "女", "birth_date": "1975-05-05",
                  "phone": f"1380001{i:04d}" if i != 5 else ""},
            headers=h,
        ).json()
        for i in range(6)
    ]
    return {"org": org, "org2": org2, "patients": patients}


# ============================================================ P0-1 报告推送调度


def test_report_push_job_idempotent_and_notifies(client, h, base):
    """同一任务同一周期只生成一份；订阅人收到站内通知。"""
    from app.database import SessionLocal
    from app.spd.jobs import spd_report_push

    template = client.post(
        "/api/spd/report-templates",
        json={"code": "rpt_p0", "name": "P0日报", "period": "daily",
              "sections": [{"key": "summary", "title": "概况"}]},
        headers=h,
    ).json()
    client.post(
        "/api/spd/report-tasks",
        json={"template_id": template["id"], "name": "P0日报推送",
              "push_time": "00:00", "subscriber_ids": [1]},  # admin 是启动种子的第一个用户
        headers=h,
    )
    with SessionLocal() as db:
        first, _ = spd_report_push(db)
        db.commit()
        second, _ = spd_report_push(db)
        db.commit()
    assert first >= 1, "到点的推送任务应生成报告"
    assert second == 0, "同一周期不得重复生成"

    instances = client.get("/api/spd/report-instances", headers=h).json()
    mine = [i for i in instances if i["title"].startswith("P0日报")]
    assert len(mine) == 1
    notices = client.get("/api/notifications", headers=h).json()
    assert any(n["category"] == "spd_report" for n in notices), "订阅人应收到报告生成通知"


def test_report_push_respects_push_time(client, h):
    """推送时点未到的任务本轮跳过（用 23:59 保证测试时点一定'未到'）。"""
    from app.database import SessionLocal
    from app.spd.jobs import spd_report_push

    template = client.post(
        "/api/spd/report-templates",
        json={"code": "rpt_p0_late", "name": "P0晚报", "period": "daily",
              "sections": [{"key": "summary"}]},
        headers=h,
    ).json()
    client.post(
        "/api/spd/report-tasks",
        json={"template_id": template["id"], "name": "P0晚报推送", "push_time": "23:59"},
        headers=h,
    )
    with SessionLocal() as db:
        spd_report_push(db)
        db.commit()
    instances = client.get("/api/spd/report-instances", headers=h).json()
    assert not any(i["title"].startswith("P0晚报") for i in instances)


# ============================================================ P0-2 三类超期一次扫


def test_sweep_marks_revisit_and_followup_overdue(client, h, base):
    """复诊与随访的逾期落 status；失访（unreachable）不被覆盖。"""
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord
    from app.spd.service import sweep_overdue

    patient = base["patients"][0]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    revisit = client.post(
        "/api/spd/revisits",
        json={"patient_id": patient["id"], "plan_date": yesterday, "items": "复查血压"},
        headers=h,
    ).json()

    with SessionLocal() as db:
        planned = SpdFollowupRecord(
            patient_id=patient["id"], org_id=base["org"]["id"],
            planned_at=yesterday, status="planned",
        )
        lost = SpdFollowupRecord(
            patient_id=patient["id"], org_id=base["org"]["id"],
            planned_at=yesterday, status="unreachable",
        )
        db.add_all([planned, lost])
        db.commit()
        result = sweep_overdue(db)
        db.commit()
        planned_id, lost_id = planned.id, lost.id

    assert result["revisits"] >= 1 and result["followups"] >= 1

    got = client.get(f"/api/spd/revisits?patient_id={patient['id']}", headers=h).json()
    mine = next(r for r in got if r["id"] == revisit["id"])
    assert mine["status"] == "overdue"
    assert any("超期扫描" in entry.get("note", "") for entry in mine["log"])

    with SessionLocal() as db:
        assert db.get(SpdFollowupRecord, planned_id).status == "overdue"
        assert db.get(SpdFollowupRecord, lost_id).status == "unreachable", \
            "失访是执行过但没联系上，不得被覆盖成超期"


# ============================================================ P0-4 宣教真外发


def test_edu_push_sms_sent_and_no_phone_failed(client, h, base):
    """有手机号 → 走短信通道置 sent；没有手机号 → failed，不静默假装送达。"""
    material = client.post(
        "/api/spd/edu-materials",
        json={"code": "edu_p0_salt", "title": "低盐饮食十讲", "content": "每天盐不超过5克",
              "program_code": "hypertension"},
        headers=h,
    ).json()
    with_phone, without_phone = base["patients"][1], base["patients"][5]
    result = client.post(
        "/api/spd/edu-pushes",
        json={"material_id": material["id"],
              "patient_ids": [with_phone["id"], without_phone["id"]], "channel": "sms"},
        headers=h,
    ).json()
    assert result == {"pushed": 2, "sent": 1, "failed": 1, "material": "低盐饮食十讲"}

    pushes = client.get(
        f"/api/spd/edu-pushes?material_id={material['id']}", headers=h
    ).json()
    by_patient = {p["patient_id"]: p["status"] for p in pushes}
    assert by_patient[with_phone["id"]] == "sent"
    assert by_patient[without_phone["id"]] == "failed"

    stats = client.get(
        "/api/spd/edu-pushes/stats?program_code=hypertension", headers=h
    ).json()
    assert stats["sent"] == 1, "已读率分母只算真发出去的"


def test_edu_push_scheduled_dispatched_by_job(client, h, base):
    """定时推送先落 pending，到点由定时任务派发。"""
    from app.database import SessionLocal
    from app.spd.jobs import spd_edu_push_dispatch

    material = client.post(
        "/api/spd/edu-materials",
        json={"code": "edu_p0_ex", "title": "运动处方入门", "content": "每周150分钟"},
        headers=h,
    ).json()
    patient = base["patients"][2]
    result = client.post(
        "/api/spd/edu-pushes",
        json={"material_id": material["id"], "patient_ids": [patient["id"]],
              "channel": "sms", "send_at": "2020-01-01 08:00:00"},
        headers=h,
    ).json()
    assert result["sent"] == 0, "定时推送不当场发"

    pushes = client.get(f"/api/spd/edu-pushes?material_id={material['id']}", headers=h).json()
    assert pushes[0]["status"] == "pending"

    with SessionLocal() as db:
        count, summary = spd_edu_push_dispatch(db)
        db.commit()
    assert count >= 1 and "派发" in summary
    pushes = client.get(f"/api/spd/edu-pushes?material_id={material['id']}", headers=h).json()
    assert pushes[0]["status"] == "sent"


# ============================================================ P0-4 呼叫通道


def test_call_task_manual_provider_stays_pending(client, h, base):
    """默认 manual 通道：受理即"等人工外呼"，任务留 pending。"""
    patient = base["patients"][1]
    result = client.post(
        "/api/spd/call-tasks",
        json={"patient_id": patient["id"], "ref_type": "followup"},
        headers=h,
    ).json()
    assert result["status"] == "pending"
    assert result["dispatch"] == {"accepted": True, "note": "待人工外呼"}


def test_call_provider_http_failure_keeps_task_pending(client, h, base, monkeypatch):
    """HTTP 通道派发失败：任务不报错、留在 pending、原因落 result。"""
    from app.spd import callcenter

    class BoomProvider:
        def dispatch(self, task_id, phone, ref_type):
            return False, "呼叫中心网关 502"

    monkeypatch.setattr(callcenter, "_provider", BoomProvider())
    try:
        result = client.post(
            "/api/spd/call-tasks",
            json={"patient_id": base["patients"][1]["id"], "ref_type": "revisit"},
            headers=h,
        ).json()
    finally:
        monkeypatch.setattr(callcenter, "_provider", None)
    assert result["status"] == "pending"
    assert result["dispatch"] == {"accepted": False, "note": "呼叫中心网关 502"}
    listed = client.get("/api/spd/call-tasks?status=pending", headers=h).json()
    assert any(t["id"] == result["id"] for t in listed)


# ============================================================ P1-1 公卫随访采集器


def test_publichealth_collector_syncs_and_is_idempotent(client, h, base):
    """公卫慢病随访的血压/血糖 → 慢专病监测数据；重复同步不产生重复行。"""
    patient = base["patients"][3]
    chronic = client.post(
        "/api/chronic",
        json={"patient_id": patient["id"], "disease": "hypertension",
              "managed_by_org_id": base["org"]["id"]},
        headers=h,
    ).json()
    client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"sbp": 162, "dbp": 96, "glucose": 5.8},
        headers=h,
    )
    source = client.post(
        "/api/spd/data-sources",
        json={"code": "ph_p1", "name": "公卫随访", "source_type": "publichealth",
              "freq_minutes": 60},
        headers=h,
    ).json()

    from app.database import SessionLocal
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource

    with SessionLocal() as db:
        src = db.get(SpdDataSource, source["id"])
        first = run_source(db, src)
        db.commit()
        assert first.success and first.rows == 3, "sbp/dbp/glucose 各一行"
        second = run_source(db, src)
        db.commit()
        assert second.success and second.rows == 0, "source_ref 判重，补跑不重复落数"

    rows = client.get(
        f"/api/spd/measurements?patient_id={patient['id']}", headers=h
    ).json()
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["bp_sys"]["value"] == 162
    assert by_metric["bp_sys"]["source"] == "publichealth"
    assert by_metric["bp_sys"]["level"] != "", "等级要按采集当时的管理目标固化"
    assert by_metric["glucose_fasting"]["program_code"] == "diabetes"


# ============================================================ P1-2 迁入确认重建责任关系


def _enroll(client, h, patient_id, org_id, program="hypertension"):
    return client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient_id, "program_code": program, "org_id": org_id},
        headers=h,
    )


def test_confirm_migration_creates_incoming_enrollment(client, h, base):
    patient = base["patients"][4]
    enrollment = _enroll(client, h, patient["id"], base["org"]["id"]).json()
    moved = client.post(
        f"/api/spd/enrollments/{enrollment['id']}/lifecycle",
        json={"event": "migrate", "reason": "搬迁随迁",
              "target_org_id": base["org2"]["id"]},
        headers=h,
    ).json()
    assert moved["pending_confirm"] is True
    assert moved["enrollment"]["status"] == "active", "未确认前档案保持在管"

    confirmed = client.post(
        f"/api/spd/lifecycle-events/{moved['event_id']}/confirm", headers=h
    ).json()
    assert confirmed["enrollment"]["status"] == "migrated"
    incoming = confirmed["incoming_enrollment"]
    assert incoming is not None and incoming["id"] != enrollment["id"]
    assert incoming["org_id"] == base["org2"]["id"]
    assert incoming["status"] == "active" and incoming["source"] == "migrate"
    assert incoming["program_code"] == "hypertension"


def test_reenroll_allowed_after_terminal_status(client, h, base):
    """部分唯一索引只锁 active：排除出组后再次纳管不再撞唯一键。"""
    patient = base["patients"][2]
    first = _enroll(client, h, patient["id"], base["org"]["id"], program="diabetes").json()
    dup = _enroll(client, h, patient["id"], base["org"]["id"], program="diabetes")
    assert dup.status_code == 409, "在管期间不得重复纳管"
    client.post(
        f"/api/spd/enrollments/{first['id']}/lifecycle",
        json={"event": "exclude", "reason": "误纳"},
        headers=h,
    )
    again = _enroll(client, h, patient["id"], base["org"]["id"], program="diabetes")
    assert again.status_code == 201, "出组后应允许重新纳管"


# ============================================================ P1-4 路径进入条件


def test_path_pauses_on_enter_condition_and_resumes(client, h, base):
    """下一节点进入条件不满足 → 实例暂停并通知；条件满足后 advance 恢复。"""
    patient = base["patients"][0]
    programs = client.get("/api/spd/programs", headers=h).json()
    program = next(p for p in programs if p["code"] == "hypertension")
    template = client.post(
        "/api/spd/path-templates",
        json={"program_id": program["id"], "code": "path_gate", "name": "条件门路径"},
        headers=h,
    ).json()
    client.post(
        f"/api/spd/path-templates/{template['id']}/nodes",
        json={"key": "n1", "name": "首次评估", "seq": 1, "next_key": "n2"},
        headers=h,
    )
    client.post(
        f"/api/spd/path-templates/{template['id']}/nodes",
        json={"key": "n2", "name": "达标复核", "seq": 2,
              "enter_condition": [{"field": "bp_sys", "op": "<", "value": 140}]},
        headers=h,
    )
    client.post(f"/api/spd/path-templates/{template['id']}/status",
                json={"status": "published"}, headers=h)

    enrollment = _enroll(client, h, patient["id"], base["org"]["id"]).json()
    instance = client.post(
        "/api/spd/path-instances",
        json={"enrollment_id": enrollment["id"], "template_id": template["id"]},
        headers=h,
    ).json()

    # 血压未达标 → 办结首节点任务后，路径应暂停而不是硬闯 n2
    client.post(
        "/api/spd/measurements",
        json={"patient_id": patient["id"], "metric": "bp_sys", "value": 158,
              "unit": "mmHg", "program_code": "hypertension"},
        headers=h,
    )
    tasks = [
        t for t in client.get(
            f"/api/spd/tasks?patient_id={patient['id']}", headers=h
        ).json()
        if t["instance_id"] == instance["id"]
    ]
    client.post(
        f"/api/spd/tasks/{tasks[0]['id']}/complete",
        json={"result": {"note": "已评估"}}, headers=h,
    )

    detail = client.get(f"/api/spd/path-instances/{instance['id']}", headers=h).json()
    assert detail["status"] == "paused"
    assert detail["current_node_key"] == "n2"

    # 条件仍不满足时不允许恢复
    blocked = client.post(
        f"/api/spd/path-instances/{instance['id']}/advance", headers=h
    )
    assert blocked.status_code == 409

    # 血压达标 → 恢复并派发 n2 任务
    client.post(
        "/api/spd/measurements",
        json={"patient_id": patient["id"], "metric": "bp_sys", "value": 132,
              "unit": "mmHg", "program_code": "hypertension"},
        headers=h,
    )
    resumed = client.post(
        f"/api/spd/path-instances/{instance['id']}/advance", headers=h
    ).json()
    assert resumed["resumed"] is True and resumed["status"] == "running"
    items = [
        t for t in client.get(
            f"/api/spd/tasks?patient_id={patient['id']}", headers=h
        ).json()
        if t["instance_id"] == instance["id"]
    ]
    assert any(t["node_key"] == "n2" for t in items), "恢复后应派发暂停节点的任务"


# ============================================================ P1-3 段落注册表


def test_unregistered_section_degrades_not_fails(client, h):
    """模板配了未注册的段落 key：报告照出，该段落提示未配置口径。"""
    template = client.post(
        "/api/spd/report-templates",
        json={"code": "rpt_unknown", "name": "含未知段落", "period": "daily",
              "sections": [{"key": "summary"}, {"key": "no_such_section", "title": "自定义"}]},
        headers=h,
    ).json()
    instance = client.post(
        "/api/spd/report-instances",
        json={"template_code": template["code"]},
        headers=h,
    ).json()
    sections = {s["key"]: s for s in instance["content"]["sections"]}
    assert "text" in sections["summary"] or "metrics" in sections["summary"]
    assert "未配置取数口径" in sections["no_such_section"]["note"]


def test_indicator_section_shares_assess_metrics(client, h, base):
    """indicator 段落复用考核指标库取数：报表与考核同源。"""
    template = client.post(
        "/api/spd/report-templates",
        json={"code": "rpt_ind", "name": "指标报告", "period": "monthly",
              "sections": [{"key": "indicator", "indicator_code": "followup_rate",
                            "title": "随访完成率"}]},
        headers=h,
    ).json()
    instance = client.post(
        "/api/spd/report-instances",
        json={"template_code": template["code"], "org_id": base["org"]["id"]},
        headers=h,
    ).json()
    section = instance["content"]["sections"][0]
    assert section["indicator_code"] == "followup_rate"
    assert "value" in section and "metrics" in section


# ============================================================ P2-3 二维码


def test_scale_qr_svg(client, h):
    """已发布量表出 SVG 二维码；未发布 409。"""
    scale = client.post(
        "/api/spd/scales",
        json={"code": "qr_scale", "name": "扫码自查量表", "category": "screen",
              "program_code": "hypertension",
              "items": [{"key": "q1", "title": "是否头晕",
                         "options": [{"label": "否", "score": 0}, {"label": "是", "score": 2}]}]},
        headers=h,
    ).json()
    blocked = client.get(f"/api/spd/scales/{scale['id']}/qr.svg", headers=h)
    assert blocked.status_code == 409, "未发布不该出码"
    client.post(f"/api/spd/scales/{scale['id']}/publish", headers=h)
    resp = client.get(f"/api/spd/scales/{scale['id']}/qr.svg", headers=h)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in resp.content

    published = next(
        s for s in client.get("/api/spd/scales?status=published", headers=h).json()
        if s["code"] == "qr_scale"
    )
    detail = client.get(
        f"/api/portal/spd/scales/by-token/{published['qr_token']}"
    )
    assert detail.status_code == 200 and detail.json()["code"] == "qr_scale", \
        "二维码令牌应能免登录换到量表"


def test_village_doctor_qr_svg(client, h, base):
    vd = client.post(
        "/api/spd/village-doctors",
        json={"user_id": 1, "org_id": base["org"]["id"], "township": "测试镇",
              "village": "测试村"},
        headers=h,
    ).json()
    resp = client.get(f"/api/spd/village-doctors/{vd['id']}/qr.svg", headers=h)
    assert resp.status_code == 200 and b"<svg" in resp.content
    client.patch(f"/api/spd/village-doctors/{vd['id']}", json={"active": False}, headers=h)
    assert client.get(f"/api/spd/village-doctors/{vd['id']}/qr.svg", headers=h).status_code == 409, \
        "停用的村医不该再出绑定码"
