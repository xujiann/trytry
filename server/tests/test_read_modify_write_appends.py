"""P1-28：读-改-写（赋值形状）里 8 处**真追加**清零后的行为回归 + 防拆卸静态钉。

八处的共同缺陷是 `obj.col = f(obj.col, 新值)`：把旧值读进 Python、算完整体写回，
并发下后写的把先写的盖掉，而两笔的日志看上去都成功了。按列的类型分三种修法：

- 字符串追加（孕产妇风险因素 ×2 / 高危儿风险备注）→ `concurrency.append_text`，
  拼接下沉到 SQL（`col || sep || :text`，空则不带分隔符）由行锁排队；
- 状态迁移 + 意见追加（处方药师审核）→ 一条带 `status = 'pending_review'` 条件的
  UPDATE（`prescriptions._apply_review`），顺带把"两位药师同时审、后者覆盖前者结论"
  的双审竞态一并关掉；
- JSON 列整体覆写（复诊日志 / 外呼结果与证据 / 召回联系记录）→ 两种方言都没有
  可移植的原子追加，进 `serialized_on` 行锁临界区、先 `db.refresh` 再追加。

SQLite 的库级写锁让并发面在 test-unit 里测不出来，八路真并发直测在
`test_postgres_real.py`（真 PG，`make test-integration`）。本文件钉两件事：
**顺序语义一字不变**（分隔符、空值不带分隔符、`[:500]` 截断、409 措辞、已高危不重复
追加）与**修法不被拆掉**（静态钉 + 八处不得再回到欠账清单）。
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import login
from test_stage14_concurrency import KNOWN_READ_MODIFY_WRITE

APP = Path(__file__).resolve().parents[1] / "app"
TODAY = date.today().isoformat()


def _patient(client, admin, name, id_card, gender="女", birth_date="1994-04-04"):
    resp = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": gender, "birth_date": birth_date,
              "phone": "138" + id_card[-8:]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _maternal_record(client, admin, patient_id, **extra):
    resp = client.post(
        "/api/maternal/records",
        json={"patient_id": patient_id, "lmp": "2026-01-10", "edc": "2026-10-17", **extra},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _record_of(client, admin, record_id):
    rows = client.get("/api/maternal/records", headers=admin).json()
    return next(r for r in rows if r["id"] == record_id)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "读改写回归院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for username, role in (("rmw_doc", "doctor"), ("rmw_pha", "pharmacist")):
        created = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role,
                  "full_name": f"读改写{role}", "org_id": org["id"]},
            headers=admin,
        )
        assert created.status_code == 201, created.text
    return {
        "org": org,
        "doctor": login(client, "rmw_doc", "pass123456"),
        "pharmacist": login(client, "rmw_pha", "pass123456"),
    }


# ---------------------------------------------------------------- 字符串追加：append_text


def test_产前筛查两项同册_风险因素两条都在(client, admin, world):
    """`create_screening`：每一项高风险/临界结论都追加进风险因素，以'；'相接、首条不带分隔符。"""
    patient = _patient(client, admin, "筛查孕妇", "330281199404041021")
    record = _maternal_record(client, admin, patient["id"])
    assert record["risk_factors"] == "" and record["high_risk"] is False

    for screen_type, result, screen_date in (("nipt", "high_risk", "2026-05-06"),
                                              ("down", "critical", "2026-04-15")):
        resp = client.post(
            "/api/maternal/screenings",
            json={"record_id": record["id"], "screen_type": screen_type,
                  "screen_date": screen_date, "result": result},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["flagged_high_risk"] is True

    after = _record_of(client, admin, record["id"])
    assert after["high_risk"] is True
    assert after["risk_factors"] == "无创产前基因检测高风险；唐氏血清学筛查临界风险"


def test_产检高血压_空档案不带分隔符_已有因素带分隔符_已高危不重复(client, admin, world):
    """`add_visit`：收缩压≥140 首次标高危时追加"妊娠期高血压可能"。

    三条顺序语义都得跟旧写法一字不差：空档案不带分隔符；已有因素以'；'相接；
    已经高危的档案再来一次高血压产检**不再追加**（`not record.high_risk` 守着）。
    """
    fresh = _maternal_record(client, admin, _patient(client, admin, "产检孕妇甲", "330281199404041032")["id"])
    receipt = client.post(
        f"/api/maternal/records/{fresh['id']}/visits",
        json={"visit_type": "prenatal", "gest_week": 20, "bp": "150/95", "visit_date": "2026-05-29"},
        headers=admin,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["high_risk"] is True
    assert _record_of(client, admin, fresh["id"])["risk_factors"] == "妊娠期高血压可能"

    again = client.post(
        f"/api/maternal/records/{fresh['id']}/visits",
        json={"visit_type": "prenatal", "gest_week": 24, "bp": "160/100", "visit_date": "2026-06-26"},
        headers=admin,
    )
    assert again.status_code == 201, again.text
    assert _record_of(client, admin, fresh["id"])["risk_factors"] == "妊娠期高血压可能", "已高危不重复追加"

    seeded = _maternal_record(
        client, admin, _patient(client, admin, "产检孕妇乙", "330281199404041043")["id"],
        risk_factors="高龄", high_risk=False,
    )
    resp = client.post(
        f"/api/maternal/records/{seeded['id']}/visits",
        json={"visit_type": "prenatal", "gest_week": 20, "bp": "142/90", "visit_date": "2026-05-29"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert _record_of(client, admin, seeded["id"])["risk_factors"] == "高龄；妊娠期高血压可能"


def test_新筛异常_风险备注追加_已高危不重复(client, admin, world):
    """`add_screening`：首次异常自动纳入高危并写风险备注；已高危的儿童再异常不重复追加。"""
    child = client.post(
        "/api/maternal/children",
        json={"name": "新筛宝宝", "gender": "男", "birth_date": "2026-08-01"},
        headers=admin,
    ).json()

    def screen(item):
        resp = client.post(
            f"/api/maternal/children/{child['id']}/screenings",
            json={"item": item, "result": "abnormal", "screen_date": "2026-08-05"},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def risk_note():
        rows = client.get("/api/maternal/children/high-risk", headers=admin).json()
        return next(r["risk_note"] for r in rows if r["id"] == child["id"])

    assert screen("hearing")["child_high_risk"] is True
    assert risk_note() == "听力筛查阳性/可疑"
    assert screen("chd")["child_high_risk"] is True
    assert risk_note() == "听力筛查阳性/可疑", "已高危的儿童不重复追加（`not child.high_risk` 守着）"


# ---------------------------------------------------------------- 条件 UPDATE：处方审核


@pytest.fixture(scope="module")
def pending_prescription(client, admin, world):
    """一条待药师审的处方：先建日剂量上限规则，再开一张超限的方。"""
    rule = client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "RMW-DRUG", "max_daily_dose": 3, "dose_unit": "g"},
        headers=admin,
    )
    assert rule.status_code == 201, rule.text
    patient = _patient(client, admin, "审方患者", "330281199404041054", gender="男")

    def prescribe():
        resp = client.post(
            "/api/prescriptions",
            json={"patient_id": patient["id"], "org_id": world["org"]["id"],
                  "diagnosis_name": "高血压",
                  "items": [{"drug_code": "RMW-DRUG", "drug_name": "回归药",
                             "daily_dose": 4, "days": 7}]},
            headers=world["doctor"],
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending_review" and "超过上限" in body["review_comment"], body
        return body

    return prescribe


def test_处方审核_意见追加在系统意见后_再审409(client, world, pending_prescription):
    rx = pending_prescription()
    reviewed = client.post(
        f"/api/prescriptions/{rx['id']}/review",
        json={"approve": True, "comment": "复核通过"},
        headers=world["pharmacist"],
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["review_comment"] == f"{rx['review_comment']}；药师意见：复核通过"

    again = client.post(
        f"/api/prescriptions/{rx['id']}/review",
        json={"approve": False, "comment": "撤回"},
        headers=world["pharmacist"],
    )
    assert again.status_code == 409, again.text
    assert again.json() == {"detail": "当前状态 approved 无需药师审核"}
    # 抢输/后到的那一路什么都不该改：结论与意见串都还是第一位药师的
    listed = client.get("/api/prescriptions?status=approved", headers=world["pharmacist"]).json()
    final = next(p for p in listed if p["id"] == rx["id"])
    assert final["review_comment"] == f"{rx['review_comment']}；药师意见：复核通过"


def test_处方审核_不带意见则意见串原样(client, world, pending_prescription):
    rx = pending_prescription()
    rejected = client.post(
        f"/api/prescriptions/{rx['id']}/review", json={"approve": False}, headers=world["pharmacist"]
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["review_comment"] == rx["review_comment"]


# ---------------------------------------------------------------- 临界区内 JSON 列追加


def test_复诊计划两次办理_两条日志都在(client, admin, world):
    """`update_revisit`：每次编辑都追加一条日志，改期 + 办结 = 两条、顺序保持。"""
    patient = _patient(client, admin, "复诊患者", "330281199404041065", gender="男")
    revisit = client.post(
        "/api/spd/revisits",
        json={"patient_id": patient["id"], "plan_date": (date.today() + timedelta(days=7)).isoformat(),
              "items": "复查血压", "source": "manual"},
        headers=admin,
    )
    assert revisit.status_code == 201, revisit.text
    rid = revisit.json()["id"]

    changed = client.patch(
        f"/api/spd/revisits/{rid}",
        json={"plan_date": (date.today() + timedelta(days=14)).isoformat(), "note": "患者外出，改期一周"},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["log"] == [{"at": TODAY, "note": "患者外出，改期一周"}]

    finished = client.patch(
        f"/api/spd/revisits/{rid}", json={"status": "done", "actual_date": TODAY}, headers=admin
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "done"
    assert finished.json()["log"] == [
        {"at": TODAY, "note": "患者外出，改期一周"},
        {"at": TODAY, "note": "状态变更为done"},
    ]


@pytest.fixture(scope="module")
def followup_record(client, admin, world):
    """一条待随访记录（经方案 → 计划两步经 HTTP 种出），供呼叫回写。"""
    rule = client.post(
        "/api/spd/followup-rules",
        json={"code": "rmw_fu_rule", "name": "读改写随访方案", "scene": "inpatient", "points": [0]},
        headers=admin,
    )
    assert rule.status_code == 201, rule.text
    patient = _patient(client, admin, "随访患者", "330281199404041076", gender="男")
    plan = client.post(
        "/api/spd/followup-plans",
        json={"patient_id": patient["id"], "rule_id": rule.json()["id"], "base_date": TODAY,
              "org_id": world["org"]["id"]},
        headers=admin,
    )
    assert plan.status_code == 201, plan.text
    record = plan.json()["items"][0]
    assert record["status"] == "planned" and record["result"] == "" and record["evidence"] == []
    return {"patient": patient, "record": record}


def _followup_record(client, admin, record_id, patient_id):
    rows = client.get("/api/spd/followup-records", params={"patient_id": patient_id}, headers=admin).json()
    return next(r for r in rows if r["id"] == record_id)


def _call_back(client, admin, patient_id, record_id, **result):
    task = client.post(
        "/api/spd/call-tasks",
        json={"patient_id": patient_id, "ref_type": "followup", "ref_id": record_id,
              "phone": "13800001111"},
        headers=admin,
    )
    assert task.status_code == 201, task.text
    resp = client.post(
        f"/api/spd/call-tasks/{task.json()['id']}/result",
        json={"status": "connected", "duration_s": 30, **result},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_呼叫结果两次回写_结果串与证据都追加_截断500(client, admin, followup_record):
    """`record_call_result`：结果串以空格相接、`strip()[:500]` 截断；证据列表逐条追加。"""
    pid, rid = followup_record["patient"]["id"], followup_record["record"]["id"]

    _call_back(client, admin, pid, rid, record_url="http://cdn/rec-1.mp3", result="已接通")
    first = _followup_record(client, admin, rid, pid)
    assert first["result"] == "已接通" and first["evidence"] == ["http://cdn/rec-1.mp3"]

    _call_back(client, admin, pid, rid, record_url="http://cdn/rec-2.mp3", result="再次接通")
    second = _followup_record(client, admin, rid, pid)
    assert second["result"] == "已接通 再次接通"
    assert second["evidence"] == ["http://cdn/rec-1.mp3", "http://cdn/rec-2.mp3"]

    _call_back(client, admin, pid, rid, result="长" * 510)  # 不带录音：证据不追加
    third = _followup_record(client, admin, rid, pid)
    assert third["result"] == ("已接通 再次接通 " + "长" * 510)[:500] and len(third["result"]) == 500
    assert third["evidence"] == ["http://cdn/rec-1.mp3", "http://cdn/rec-2.mp3"]


def test_呼叫结果只回写待随访的记录(client, admin, world, followup_record):
    """已完成的随访记录不再被外呼结果改写（临界区内按重读后的状态再判一次）。"""
    pid, rid = followup_record["patient"]["id"], followup_record["record"]["id"]
    executed = client.post(
        f"/api/spd/followup-records/{rid}/execute",
        json={"answers": {}, "channel": "phone", "result": "随访完成"},
        headers=admin,
    )
    assert executed.status_code == 200, executed.text
    done = _followup_record(client, admin, rid, pid)
    assert done["status"] == "done"

    _call_back(client, admin, pid, rid, record_url="http://cdn/late.mp3", result="迟到的回写")
    after = _followup_record(client, admin, rid, pid)
    assert after["result"] == done["result"] and after["evidence"] == done["evidence"]


def test_召回两次留痕_两条联系记录都在(client, admin, world):
    """`update_recall`：每次带联系备注的进展都追加一条联系记录；结果为空时保留旧结果。"""
    patient = _patient(client, admin, "召回患者", "330281199404041087", gender="男")
    enrollment = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension", "org_id": world["org"]["id"]},
        headers=admin,
    )
    assert enrollment.status_code == 201, enrollment.text
    recalled = client.post(
        f"/api/spd/enrollments/{enrollment.json()['id']}/lifecycle",
        json={"event": "recall", "reason": "失访三月"},
        headers=admin,
    )
    assert recalled.status_code == 200, recalled.text
    recalls = client.get("/api/spd/recalls", headers=admin).json()
    rid = next(r["id"] for r in recalls if r["enrollment_id"] == enrollment.json()["id"])

    def progress(**body):
        resp = client.post(f"/api/spd/recalls/{rid}/progress", json=body, headers=admin)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def recall():
        return next(r for r in client.get("/api/spd/recalls", headers=admin).json() if r["id"] == rid)

    progress(status="contacted", contact_note="电话已接", result="愿意复诊")
    progress(status="contacted", contact_note="约定周五复诊")  # result 为空：保留旧结果
    current = recall()
    assert current["contacts"] == [{"at": TODAY, "note": "电话已接"}, {"at": TODAY, "note": "约定周五复诊"}]
    assert current["result"] == "愿意复诊"
    assert progress(status="returned", result="已回访") == {"id": rid, "status": "returned", "result": "已回访"}
    assert recall()["contacts"] == [{"at": TODAY, "note": "电话已接"}, {"at": TODAY, "note": "约定周五复诊"}]


# ---------------------------------------------------------------- 防拆卸


def test_八处修法不得被拆掉_静态钉():
    """三种修法各自的形状必须还在源码里：谁把它改回 `obj.col = f(obj.col, x)`，这里先红。"""
    maternal = (APP / "routers" / "maternal.py").read_text(encoding="utf-8")
    assert maternal.count('append_text(db, MaternalRecord, ') == 1, "产前筛查风险因素追加（无守卫，纯追加）"
    # 产检高血压 / 新筛异常带"只记第一次"守卫：判定必须压进同一条 UPDATE 的 WHERE 里，
    # 留在 Python 侧的 `if not x.high_risk` 是 check-then-act（/review 指出）
    assert maternal.count('_mark_high_risk(db, MaternalRecord, ') == 1
    assert maternal.count('_mark_high_risk(db, ChildRecord, ') == 1
    assert "model.high_risk.is_(False)" in maternal and "appended_text(column, factor)" in maternal

    prescriptions = (APP / "routers" / "prescriptions.py").read_text(encoding="utf-8")
    assert "update(Prescription)" in prescriptions
    assert 'Prescription.status == "pending_review"' in prescriptions, "状态条件必须压在 UPDATE 的 WHERE 里"
    assert "appended_text(Prescription.review_comment" in prescriptions

    spd = APP / "spd" / "routers"
    for filename, model in (("care.py", "SpdRevisit"), ("followup.py", "SpdFollowupRecord"),
                            ("population.py", "SpdRecall")):
        source = (spd / filename).read_text(encoding="utf-8")
        assert f"with serialized_on(db, {model}, " in source, f"{filename} 的 JSON 列追加必须在行锁临界区内"

    billing = (APP / "routers" / "billing.py").read_text(encoding="utf-8")
    assert "def _serialized_on" not in billing, "闸门已上提为 concurrency.serialized_on，别再复制一份私有的"
    assert billing.count("with serialized_on(db, ") == 4


def test_八处不得再回到欠账清单():
    """清零的八处不许再登记回 KNOWN_READ_MODIFY_WRITE；`record_call_result` 只剩两条幂等回填。"""
    cleared = {
        "billing.py:refund_payment", "maternal.py:add_visit", "maternal.py:add_screening",
        "maternal.py:create_screening", "prescriptions.py:review_prescription",
        "spd/care.py:update_revisit", "spd/population.py:update_recall",
    }
    assert not cleared & set(KNOWN_READ_MODIFY_WRITE), cleared & set(KNOWN_READ_MODIFY_WRITE)
    assert KNOWN_READ_MODIFY_WRITE["spd/followup.py:record_call_result"][0] <= 2
