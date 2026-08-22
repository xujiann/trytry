"""全域慢专病全流程管理系统：配置 → 筛查 → 纳管 → 路径任务 → 转诊 → 考核 主链路。

这套用例按业务链路而不是按接口组织：单接口 200 说明不了问题，
"筛出来的人能不能被管起来、管起来的人任务会不会自动生成、任务办完路径会不会走"
才是这套系统真正要保证的东西。
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
    county = client.post(
        "/api/organizations",
        json={"name": "慢专病县医院", "org_type": "lead_hospital", "level": "county"},
        headers=h,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "慢专病卫生院", "org_type": "township", "level": "township",
              "parent_id": county["id"]},
        headers=h,
    ).json()
    village = client.post(
        "/api/organizations",
        json={"name": "慢专病村卫生室", "org_type": "village", "level": "village",
              "parent_id": township["id"]},
        headers=h,
    ).json()
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"专病患者{i}", "id_card": f"33018219800202{i:04d}",
                  "gender": "男", "birth_date": "1980-02-02", "phone": f"1390000{i:04d}"},
            headers=h,
        ).json()
        for i in range(6)
    ]
    return {"county": county, "township": township, "village": village, "patients": patients}


# ============================================================ 种子与配置


def test_seed_programs_and_targets(client, h):
    programs = client.get("/api/spd/programs", headers=h).json()
    codes = {p["code"] for p in programs}
    assert {"hypertension", "diabetes", "stroke", "chd", "copd", "ckd"} <= codes
    assert {"af", "osteoporosis"} <= codes, "专病口径病种也应种子化"
    hypertension = next(p for p in programs if p["code"] == "hypertension")
    assert hypertension["include_rules"], "病种应带纳入规则，否则无法自动识别"
    assert hypertension["stages"], "病种应带管理阶段"

    detail = client.get(f"/api/spd/programs/{hypertension['id']}", headers=h).json()
    metrics = {t["metric"] for t in detail["targets"]}
    assert {"bp_sys", "bp_dia"} <= metrics
    assert any(t["kind"] == "qualitative" for t in detail["targets"]), "应有定性目标"


def test_seed_scales_indicators_rules(client, h):
    scales = client.get("/api/spd/scales?status=published", headers=h).json()
    assert {"scr_hypertension", "scr_diabetes", "scr_stroke"} <= {s["code"] for s in scales}
    indicators = client.get("/api/spd/indicators?limit=100", headers=h).json()
    assert {"followup_rate", "referral_closure_rate", "vd_referral_up"} <= {
        i["code"] for i in indicators
    }
    rules = client.get("/api/spd/followup-rules", headers=h).json()
    assert {"fr_discharge", "fr_surgery"} <= {r["code"] for r in rules}
    questionnaires = client.get("/api/spd/questionnaires", headers=h).json()
    assert "q_discharge" in {q["code"] for q in questionnaires}


def test_program_rule_change_bumps_version_and_keeps_snapshot(client, h):
    program = client.post(
        "/api/spd/programs",
        json={"code": "test_prog", "name": "测试病种", "category": "specialty",
              "include_rules": [{"field": "age", "op": ">=", "value": 60}]},
        headers=h,
    ).json()
    assert program["version"] == "v1"
    updated = client.patch(
        f"/api/spd/programs/{program['id']}",
        json={"include_rules": [{"field": "age", "op": ">=", "value": 65}], "note": "上调年龄"},
        headers=h,
    ).json()
    assert updated["version"] == "v2", "改规则应升版本"
    versions = client.get(f"/api/spd/programs/{program['id']}/versions", headers=h).json()
    assert versions and versions[0]["snapshot"]["include_rules"][0]["value"] == 60


def test_invalid_rule_rejected(client, h):
    resp = client.post(
        "/api/spd/programs",
        json={"code": "bad_prog", "name": "坏规则", "category": "chronic",
              "include_rules": [{"field": "age", "op": "≥", "value": 60}]},
        headers=h,
    )
    assert resp.status_code == 422
    assert "比较符" in resp.json()["detail"]


# ============================================================ 筛查 → 目标池


def test_screening_with_scale_creates_candidate(client, h, base):
    patient = base["patients"][0]
    resp = client.post(
        "/api/spd/screenings",
        json={
            "patient_id": patient["id"], "program_code": "hypertension",
            "source": "opportunistic", "org_id": base["township"]["id"],
            "scale_code": "scr_hypertension",
            "answers": {"family": "是", "salt": "是", "overweight": "是",
                        "smoke": "是", "drink": "否", "symptom": "是"},
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["risk_level"] == "high"
    assert body["result"] == "suspect"

    candidates = client.get(
        "/api/spd/candidates?program_code=hypertension", headers=h
    ).json()
    assert any(c["patient_id"] == patient["id"] for c in candidates)


def test_low_risk_screening_stays_normal(client, h, base):
    patient = base["patients"][1]
    body = client.post(
        "/api/spd/screenings",
        json={"patient_id": patient["id"], "program_code": "diabetes",
              "org_id": base["township"]["id"], "scale_code": "scr_diabetes",
              "answers": {"age": "否", "family": "否", "overweight": "否",
                          "inactive": "否", "gdm": "否", "symptom": "否"}},
        headers=h,
    ).json()
    assert body["risk_level"] == "low"
    assert body["result"] == "normal"
    candidates = client.get("/api/spd/candidates?program_code=diabetes", headers=h).json()
    assert not any(c["patient_id"] == patient["id"] for c in candidates)


def test_high_risk_review_promotes_to_target(client, h, base):
    screenings = client.get(
        "/api/spd/screenings?program_code=hypertension&reviewed=false", headers=h
    ).json()
    assert screenings
    resp = client.post(
        f"/api/spd/screenings/{screenings[0]['id']}/review",
        json={"review_result": "confirmed", "review_note": "复核确认"},
        headers=h,
    )
    assert resp.status_code == 200
    candidates = client.get(
        "/api/spd/candidates?program_code=hypertension&status=target", headers=h
    ).json()
    assert candidates, "复核确认后应进入目标人群"


def test_auto_screen_requires_include_rules(client, h, base):
    program = client.post(
        "/api/spd/programs",
        json={"code": "no_rule_prog", "name": "无规则病种", "category": "chronic"},
        headers=h,
    ).json()
    assert program["code"] == "no_rule_prog"
    resp = client.post(
        "/api/spd/screenings/auto-run",
        json={"program_code": "no_rule_prog", "org_id": base["township"]["id"]},
        headers=h,
    )
    assert resp.status_code == 422
    assert "纳入规则" in resp.json()["detail"]


def test_auto_screen_matches_by_diagnosis(client, h, base):
    """有 I10 诊断的患者应被高血压纳入规则自动识别。"""
    patient = base["patients"][2]
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": base["township"]["id"],
              "encounter_type": "outpatient", "diagnosis_code": "I10",
              "diagnosis_name": "原发性高血压"},
        headers=h,
    )
    resp = client.post(
        "/api/spd/screenings/auto-run",
        json={"program_code": "hypertension", "org_id": base["township"]["id"]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scanned"] >= 1
    assert body["suspect"] >= 1
    candidates = client.get(
        "/api/spd/candidates?program_code=hypertension", headers=h
    ).json()
    assert any(c["patient_id"] == patient["id"] for c in candidates)


# ============================================================ 团队与纳管


@pytest.fixture(scope="module")
def team(client, h, base):
    return client.post(
        "/api/spd/teams",
        json={"name": "高血压服务团队", "org_id": base["township"]["id"],
              "level": "township", "program_codes": ["hypertension"]},
        headers=h,
    ).json()


def test_enrollment_flow(client, h, base, team):
    patient = base["patients"][0]
    resp = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": base["township"]["id"], "team_id": team["id"],
              "risk_level": "high", "sign_date": date.today().isoformat(),
              "consent_signed": True, "service_start": date.today().isoformat(),
              "habits": {"smoke": "是"}, "risk_factors": ["高盐饮食"]},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    enrollment = resp.json()
    assert enrollment["status"] == "active"
    assert enrollment["archived"] is True, "填了生活习惯/危险因素即视为已建档"
    assert enrollment["stage"], "应按病种首个阶段自动落位"

    candidates = client.get(
        "/api/spd/candidates?program_code=hypertension", headers=h
    ).json()
    hit = next(c for c in candidates if c["patient_id"] == patient["id"])
    assert hit["status"] == "enrolled", "纳管后目标池状态应同步"


@pytest.fixture(scope="module")
def enrollment(client, h, base, team):
    """保证存在一份高血压在管档案。

    本模块有五处 `GET /api/spd/enrollments?program_code=hypertension` 之后直接取 `[0]`，
    原先靠 `test_enroll_*` 排在前面顺带造出来。整模块跑得过，`pytest -k` 只选中
    其中一条时列表是空的、`[0]` 直接 IndexError——用例之间不该有这种看不见的先后依赖。

    幂等：已经有就直接返回，不重复建（重复建会撞唯一约束 409）。
    """
    from datetime import date

    existing = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()
    if existing:
        return existing[0]
    resp = client.post(
        "/api/spd/enrollments",
        json={"patient_id": base["patients"][0]["id"], "program_code": "hypertension",
              "org_id": base["township"]["id"], "team_id": team["id"],
              "risk_level": "high", "sign_date": date.today().isoformat(),
              "consent_signed": True, "service_start": date.today().isoformat()},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_duplicate_enrollment_rejected(client, h, base, team):
    resp = client.post(
        "/api/spd/enrollments",
        json={"patient_id": base["patients"][0]["id"], "program_code": "hypertension",
              "org_id": base["township"]["id"]},
        headers=h,
    )
    assert resp.status_code == 409


def test_multi_program_enrollment_allowed(client, h, base, team):
    """一人多病是慢病常态：同一患者可同时纳管多个病种。"""
    resp = client.post(
        "/api/spd/enrollments",
        json={"patient_id": base["patients"][0]["id"], "program_code": "diabetes",
              "org_id": base["township"]["id"], "risk_level": "mid"},
        headers=h,
    )
    assert resp.status_code == 201
    profile = client.get(
        f"/api/spd/patients/{base['patients'][0]['id']}/profile", headers=h
    ).json()
    assert len(profile["programs"]) >= 2


# ============================================================ 路径与任务


@pytest.fixture(scope="module")
def path_template(client, h):
    programs = client.get("/api/spd/programs?keyword=高血压", headers=h).json()
    program = next(p for p in programs if p["code"] == "hypertension")
    template = client.post(
        "/api/spd/path-templates",
        json={"program_id": program["id"], "code": "hp_std", "name": "高血压标准路径",
              "scene": "outpatient"},
        headers=h,
    ).json()
    for seq, (key, name, nxt) in enumerate([
        ("assess", "初次评估", "target"),
        ("target", "制定管理目标", "followup"),
        ("followup", "首次随访", ""),
    ]):
        client.post(
            f"/api/spd/path-templates/{template['id']}/nodes",
            json={"key": key, "name": name, "seq": seq, "next_key": nxt,
                  "due_days": 7, "service_type": "followup"},
            headers=h,
        )
    # 发布掉：`path-instances` 只收已发布的模板。原先这一步靠
    # `test_publish_*` 顺带做掉，于是 `-k start_path` 单选时模板还是草稿，
    # 建实例被 422 拒。"可用的模板"本就该由建它的 fixture 负责交付。
    published = client.post(
        f"/api/spd/path-templates/{template['id']}/status",
        json={"status": "published"}, headers=h,
    )
    assert published.status_code == 200, published.text
    return template


def test_publish_rejects_dangling_next_key(client, h, path_template):
    programs = client.get("/api/spd/programs", headers=h).json()
    program = next(p for p in programs if p["code"] == "diabetes")
    broken = client.post(
        "/api/spd/path-templates",
        json={"program_id": program["id"], "code": "dm_broken", "name": "断头路径"},
        headers=h,
    ).json()
    client.post(
        f"/api/spd/path-templates/{broken['id']}/nodes",
        json={"key": "a", "name": "节点A", "next_key": "nowhere"},
        headers=h,
    )
    resp = client.post(
        f"/api/spd/path-templates/{broken['id']}/status",
        json={"status": "published"}, headers=h,
    )
    assert resp.status_code == 422
    assert "下一节点不存在" in resp.json()["detail"]


def test_publish_empty_path_rejected(client, h):
    programs = client.get("/api/spd/programs", headers=h).json()
    program = next(p for p in programs if p["code"] == "copd")
    empty = client.post(
        "/api/spd/path-templates",
        json={"program_id": program["id"], "code": "copd_empty", "name": "空路径"},
        headers=h,
    ).json()
    resp = client.post(
        f"/api/spd/path-templates/{empty['id']}/status",
        json={"status": "published"}, headers=h,
    )
    assert resp.status_code == 422


def test_published_path_nodes_immutable(client, h, path_template):
    published = client.post(
        f"/api/spd/path-templates/{path_template['id']}/status",
        json={"status": "published"}, headers=h,
    )
    assert published.status_code == 200
    resp = client.post(
        f"/api/spd/path-templates/{path_template['id']}/nodes",
        json={"key": "extra", "name": "偷加的节点"}, headers=h,
    )
    assert resp.status_code == 409, "已发布路径不可直接加节点"

    copy = client.post(
        f"/api/spd/path-templates/{path_template['id']}/copy",
        json={"code": "hp_std_v2"}, headers=h,
    )
    assert copy.status_code == 201
    assert copy.json()["status"] == "draft"
    detail = client.get(f"/api/spd/path-templates/{copy.json()['id']}", headers=h).json()
    assert len(detail["nodes"]) == 3, "复制应连节点一起复制"


def test_start_path_generates_first_task(client, h, base, path_template, enrollment):
    enrollments = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()
    enrollment = enrollments[0]
    resp = client.post(
        "/api/spd/path-instances",
        json={"enrollment_id": enrollment["id"], "template_id": path_template["id"]},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    instance = resp.json()
    assert instance["current_node_key"] == "assess"

    tasks = client.get(
        f"/api/spd/tasks?patient_id={enrollment['patient_id']}&task_type=path", headers=h
    ).json()
    assert tasks, "启动路径应派生首节点任务"
    assert tasks[0]["status"] == "pending"

    dup = client.post(
        "/api/spd/path-instances",
        json={"enrollment_id": enrollment["id"], "template_id": path_template["id"]},
        headers=h,
    )
    assert dup.status_code == 409, "同一模板不得重复启动"


@pytest.fixture(scope="module")
def path_instance(client, h, path_template, enrollment):
    """保证这份在管档案上已经启动了路径（于是有首节点任务可做）。

    幂等：已启动就直接返回既有实例（重复启动会被 409 拒）。
    """
    resp = client.post(
        "/api/spd/path-instances",
        json={"enrollment_id": enrollment["id"], "template_id": path_template["id"]},
        headers=h,
    )
    if resp.status_code == 201:
        return resp.json()
    instances = client.get(
        f"/api/spd/path-instances?enrollment_id={enrollment['id']}", headers=h
    ).json()
    assert instances, f"路径既未启动成功也查不到既有实例：{resp.status_code} {resp.text}"
    return instances[0]


def test_task_completion_advances_path(client, h, base, enrollment, path_instance):
    enrollment = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()[0]
    tasks = client.get(
        f"/api/spd/tasks?patient_id={enrollment['patient_id']}&task_type=path&open_only=true",
        headers=h,
    ).json()
    task = tasks[0]
    client.post(f"/api/spd/tasks/{task['id']}/claim", headers=h)
    done = client.post(
        f"/api/spd/tasks/{task['id']}/complete",
        json={"result": {"note": "已评估"}}, headers=h,
    )
    assert done.status_code == 200, done.text
    assert done.json()["advanced"]["current_node_key"] == "target"

    instances = client.get(
        f"/api/spd/path-instances?enrollment_id={enrollment['id']}", headers=h
    ).json()
    assert instances[0]["current_node_key"] == "target"
    assert instances[0]["progress"] > 0

    next_tasks = client.get(
        f"/api/spd/tasks?patient_id={enrollment['patient_id']}&task_type=path&open_only=true",
        headers=h,
    ).json()
    assert any(t["node_key"] == "target" for t in next_tasks), "应自动派生下一节点任务"


def test_evidence_required_blocks_completion(client, h, base):
    """佐证收紧为真实附件：任意字符串糊弄不过去，别的任务的附件也不行。"""
    patient = base["patients"][3]
    task = client.post(
        "/api/spd/tasks",
        json={"patient_id": patient["id"], "title": "需佐证的任务",
              "task_type": "report", "require_evidence": True},
        headers=h,
    ).json()
    resp = client.post(
        f"/api/spd/tasks/{task['id']}/complete", json={"result": {}}, headers=h
    )
    assert resp.status_code == 422

    fake = client.post(
        f"/api/spd/tasks/{task['id']}/complete",
        json={"result": {}, "evidence": ["/uploads/a.jpg"]}, headers=h,
    )
    assert fake.status_code == 422, "自由字符串不再能过 require_evidence"

    upload = client.post(
        "/api/attachments",
        files={"file": ("bp.png", b"\x89PNG\r\n\x1a\n" + b"fake-bytes", "image/png")},
        data={"owner_type": "spd_task", "owner_id": str(task["id"])},
        headers=h,
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["id"]

    other = client.post(
        "/api/spd/tasks",
        json={"patient_id": patient["id"], "title": "别的任务",
              "task_type": "report", "require_evidence": True},
        headers=h,
    ).json()
    stolen = client.post(
        f"/api/spd/tasks/{other['id']}/complete",
        json={"result": {}, "evidence": [attachment_id]}, headers=h,
    )
    assert stolen.status_code == 422, "别的任务的附件不能拿来当自己的佐证"

    ok = client.post(
        f"/api/spd/tasks/{task['id']}/complete",
        json={"result": {}, "evidence": [attachment_id]}, headers=h,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["evidence_urls"][0]["url"] == f"/api/attachments/{attachment_id}"


def test_task_claim_conflict_and_batch(client, h, base):
    patient = base["patients"][4]
    ids = []
    for i in range(3):
        ids.append(client.post(
            "/api/spd/tasks",
            json={"patient_id": patient["id"], "title": f"批量任务{i}",
                  "task_type": "followup"},
            headers=h,
        ).json()["id"])
    resp = client.post(
        "/api/spd/tasks/batch",
        json={"task_ids": ids, "action": "urge"}, headers=h,
    )
    assert resp.json()["processed"] == 3
    cancelled = client.post(
        "/api/spd/tasks/batch",
        json={"task_ids": ids, "action": "cancel", "note": "重复建单"}, headers=h,
    )
    assert cancelled.json()["processed"] == 3
    again = client.post(
        "/api/spd/tasks/batch", json={"task_ids": ids, "action": "urge"}, headers=h
    )
    assert again.json()["processed"] == 0
    assert len(again.json()["skipped"]) == 3, "已结束任务应逐条跳过而不是整批失败"


def test_task_export(client, h):
    export = client.get("/api/spd/tasks-export?limit=50", headers=h).json()
    assert export["columns"][0] == "任务ID"
    assert isinstance(export["rows"], list)


# ============================================================ 监测与评估


def test_measurement_level_and_abnormal_task(client, h, base, enrollment):
    enrollment = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()[0]
    before = len(client.get(
        f"/api/spd/tasks?patient_id={enrollment['patient_id']}&task_type=intervention",
        headers=h,
    ).json())
    resp = client.post(
        "/api/spd/measurements",
        json={"patient_id": enrollment["patient_id"], "metric": "bp_sys",
              "value": 178, "unit": "mmHg", "program_code": "hypertension"},
        headers=h,
    )
    assert resp.status_code == 201
    assert resp.json()["level"] == "high", "超过管理目标上限应判为偏高"
    after = client.get(
        f"/api/spd/tasks?patient_id={enrollment['patient_id']}&task_type=intervention",
        headers=h,
    ).json()
    assert len(after) == before + 1, "指标异常应自动派处置任务"

    normal = client.post(
        "/api/spd/measurements",
        json={"patient_id": enrollment["patient_id"], "metric": "bp_sys",
              "value": 128, "unit": "mmHg", "program_code": "hypertension"},
        headers=h,
    ).json()
    assert normal["level"] == "normal"

    trend = client.get(
        f"/api/spd/measurements/trend?patient_id={enrollment['patient_id']}&metric=bp_sys",
        headers=h,
    ).json()
    assert trend["total"] >= 2
    assert trend["points"]


def test_assessment_updates_risk_level(client, h, base):
    enrollment = client.get(
        "/api/spd/enrollments?program_code=diabetes", headers=h
    ).json()[0]
    resp = client.post(
        "/api/spd/assessments",
        json={"patient_id": enrollment["patient_id"], "scale_code": "assess_risk_common",
              "program_code": "diabetes",
              "answers": {"control": "未达标", "adherence": "差",
                          "complication": "2项及以上", "selfcare": "完全依赖"}},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["risk_level"] == "very_high"
    refreshed = client.get(f"/api/spd/enrollments/{enrollment['id']}", headers=h).json()
    assert refreshed["risk_level"] == "very_high", "评估结论应回写档案风险等级"

    revisits = client.get(
        f"/api/spd/revisits?patient_id={enrollment['patient_id']}", headers=h
    ).json()
    assert any(r["source"] == "high_risk" for r in revisits), "高危应自动排复诊"


def test_unpublished_scale_cannot_be_used(client, h, base):
    draft = client.post(
        "/api/spd/scales",
        json={"code": "draft_scale", "name": "草稿量表",
              "items": [{"key": "q1", "title": "题", "type": "single",
                         "options": [{"label": "是", "score": 1}]}]},
        headers=h,
    ).json()
    assert draft["status"] == "draft"
    resp = client.post(
        "/api/spd/assessments",
        json={"patient_id": base["patients"][0]["id"], "scale_code": "draft_scale",
              "answers": {"q1": "是"}},
        headers=h,
    )
    assert resp.status_code == 404


# ============================================================ 逐级转诊


def test_referral_chain_to_closure(client, h, base, enrollment):
    enrollment = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()[0]
    case = client.post(
        "/api/spd/referrals",
        json={"patient_id": enrollment["patient_id"], "program_code": "hypertension",
              "direction": "up", "target_org_id": base["county"]["id"],
              "reason": "血压持续不达标"},
        headers=h,
    )
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    assert case.json()["status"] == "submitted"

    # ADR-0005 三级链：两步审核（卫生院审核 → 县级接收）
    for expected in ("township_reviewed", "accepted"):
        resp = client.post(
            f"/api/spd/referrals/{case_id}/review",
            json={"action": "pass", "opinion": "同意"}, headers=h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == expected

    arrived = client.post(
        f"/api/spd/referrals/{case_id}/arrive",
        json={"effective_visit": True}, headers=h,
    ).json()
    assert arrived["effective_visit"] is True

    down = client.post(
        f"/api/spd/referrals/{case_id}/down",
        json={"target_org_id": base["township"]["id"], "stable": True}, headers=h,
    ).json()
    assert down["status"] == "down_referred"

    closed = client.post(
        f"/api/spd/referrals/{case_id}/receive-followup",
        json={"opinion": "已接收随访"}, headers=h,
    ).json()
    assert closed["status"] == "closed"

    detail = client.get(f"/api/spd/referrals/{case_id}", headers=h).json()
    steps = [s["step"] for s in detail["steps"]]
    assert steps == ["发起", "卫生院审核", "县级医院接收", "到院", "下转", "随访接收"]

    stats = client.get("/api/spd/referrals-stats/closure", headers=h).json()
    assert stats["closed"] >= 1
    assert stats["effective_visits"] >= 1


def test_referral_reject_and_terminal_guard(client, h, base):
    enrollment = client.get(
        "/api/spd/enrollments?program_code=diabetes", headers=h
    ).json()[0]
    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": enrollment["patient_id"], "program_code": "diabetes",
              "reason": "血糖失控"},
        headers=h,
    ).json()["id"]
    rejected = client.post(
        f"/api/spd/referrals/{case_id}/review",
        json={"action": "reject", "opinion": "基层可控"}, headers=h,
    ).json()
    assert rejected["status"] == "rejected"
    again = client.post(
        f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=h
    )
    assert again.status_code == 409, "终态单据不接受推进"


def test_referral_review_requires_parent_org(client, h, base):
    """越权修复回归：分级审核只能由本单当前机构的直接上级（parent_id）推进。

    树：village.parent=township，township.parent=county。村卫生室发起的单子，
    步骤一"卫生院审核"须由 village 的上级 township 推进；county（非直接上级）
    与无关机构均应 403。admin/director 全域角色不在此测（其放行由既有
    test_referral_chain_to_closure 覆盖）。
    """
    village_id = base["village"]["id"]
    township_id = base["township"]["id"]
    county_id = base["county"]["id"]

    # 独立患者并纳管在 village，给村医visibility；作为 village 发起单据的当前机构
    patient = client.post(
        "/api/patients",
        json={"name": "越权测试患者", "id_card": "330182198203030099", "phone": "13900009099"},
        headers=h,
    ).json()
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": village_id, "risk_level": "high"},
        headers=h,
    )

    def _mk_doctor(username, org_id):
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": "doctor",
                  "org_id": org_id},
            headers=h,
        )
        r = client.post("/api/auth/login", json={"username": username, "password": "pass123456"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    vdoc = _mk_doctor("ref_vdoc", village_id)      # 村医：发起
    tdoc = _mk_doctor("ref_tdoc", township_id)     # 卫生院：village 的直接上级 → 有权复核
    cdoc = _mk_doctor("ref_cdoc", county_id)       # 县医院：越级，非 village 的直接上级

    case = client.post(
        "/api/spd/referrals",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "direction": "up", "reason": "血压持续不达标"},
        headers=vdoc,
    )
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    assert case.json()["current_org_id"] == village_id
    assert case.json()["status"] == "submitted"

    # 越级：county 不是 village 的直接上级 → 403
    bad = client.post(
        f"/api/spd/referrals/{case_id}/review",
        json={"action": "pass", "opinion": "越级审核"}, headers=cdoc,
    )
    assert bad.status_code == 403, bad.text

    # 发起人本级（村医自己）也不能自审推进 → 403
    self_rev = client.post(
        f"/api/spd/referrals/{case_id}/review",
        json={"action": "pass"}, headers=vdoc,
    )
    assert self_rev.status_code == 403, self_rev.text

    # 直接上级 township 审核 → 放行（ADR-0005 三级链步骤一）
    ok = client.post(
        f"/api/spd/referrals/{case_id}/review",
        json={"action": "pass", "opinion": "同意上转"}, headers=tdoc,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "township_reviewed"
    assert ok.json()["current_org_id"] == township_id

    # 第二跳：现当前机构=township，其直接上级=county → county 医生可接收；township 自审 403
    assert client.post(
        f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=tdoc,
    ).status_code == 403, "township 已是当前机构，不能自审推进下一格"
    hop2 = client.post(
        f"/api/spd/referrals/{case_id}/review",
        json={"action": "pass", "opinion": "县级接收"}, headers=cdoc,
    )
    assert hop2.status_code == 200, hop2.text
    assert hop2.json()["status"] == "accepted"
    assert hop2.json()["current_org_id"] == county_id


def test_referral_global_review_keeps_org_anchor(client, h, base):
    """回归 ADR-0004：全域角色（admin，无绑定机构）代推进后不得把机构锚点清成 None，
    否则后续环节的 parent 校验会把所有非全域账号锁死。"""
    village_id = base["village"]["id"]
    township_id = base["township"]["id"]

    patient = client.post(
        "/api/patients",
        json={"name": "锚点测试患者", "id_card": "330182198204040098", "phone": "13900009098"},
        headers=h,
    ).json()
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": village_id, "risk_level": "high"},
        headers=h,
    )
    client.post(
        "/api/users",
        json={"username": "ref_tdoc2", "password": "pass123456", "role": "doctor",
              "org_id": township_id},
        headers=h,
    )
    tdoc = client.post(
        "/api/auth/login", json={"username": "ref_tdoc2", "password": "pass123456"}
    ).json()
    tdoc = {"Authorization": f"Bearer {tdoc['access_token']}"}

    # village 发起（admin 代发，其 org_id=None，回落到 enrollment 的 village）
    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": patient["id"], "program_code": "hypertension", "reason": "上转"},
        headers=h,
    ).json()["id"]

    # admin（全域）做步骤一：不应把 current_org_id 清成 None
    step1 = client.post(
        f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=h
    ).json()
    assert step1["status"] == "township_reviewed"
    assert step1["current_org_id"] == village_id, "全域代推进须保留机构锚点，不能清 None"

    # 非全域的 township（village 的直接上级）仍可继续推进，未被锁死
    step2 = client.post(
        f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=tdoc
    )
    assert step2.status_code == 200, step2.text
    assert step2.json()["status"] == "accepted"


def test_referral_arrive_down_receive_require_current_org(client, h):
    """回归 ADR-0004 后续③：到院/下转/随访接收只有本单当前持有机构可操作。

    用标准**三级机构树**（county←township←village，ADR-0005 口径）让审核链由机构账号
    逐级推进到 accepted——此时 current_org 正确落在受理的 county（受理机构，而非发起
    村室）。随后：非当前机构 403、当前机构放行；下转把 current_org 切到下转目标，
    接收随之易主。
    """
    def _org(name, level, org_type, parent_id=None):
        body = {"name": name, "org_type": org_type, "level": level}
        if parent_id is not None:
            body["parent_id"] = parent_id
        return client.post("/api/organizations", json=body, headers=h).json()

    county = _org("持有县医院", "county", "lead_hospital")
    township = _org("持有卫生院", "township", "township", county["id"])
    village = _org("持有村卫生室", "village", "village", township["id"])

    def _doctor(username, org_id):
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": "doctor",
                  "org_id": org_id},
            headers=h,
        )
        r = client.post("/api/auth/login", json={"username": username, "password": "pass123456"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    vdoc = _doctor("hold_vdoc", village["id"])       # 发起
    tdoc = _doctor("hold_tdoc", township["id"])      # 卫生院审核
    cdoc = _doctor("hold_cdoc", county["id"])        # 县医院接收

    patient = client.post(
        "/api/patients",
        json={"name": "持有机构测试", "id_card": "330182198205050097", "phone": "13900009097"},
        headers=h,
    ).json()
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": village["id"], "risk_level": "high"},
        headers=h,
    )

    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": patient["id"], "program_code": "hypertension", "reason": "上转"},
        headers=vdoc,
    ).json()["id"]
    # 机构账号逐级推进到 accepted（三级链两步审核）：township → county
    for reviewer in (tdoc, cdoc):
        r = client.post(f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=reviewer)
        assert r.status_code == 200, r.text
    acc = client.get(f"/api/spd/referrals/{case_id}", headers=h).json()
    assert acc["status"] == "accepted" and acc["current_org_id"] == county["id"]

    # 到院：非当前机构（village）403，当前受理机构（county）放行
    assert client.post(
        f"/api/spd/referrals/{case_id}/arrive", json={"effective_visit": True}, headers=vdoc
    ).status_code == 403
    arr = client.post(
        f"/api/spd/referrals/{case_id}/arrive", json={"effective_visit": True}, headers=cdoc
    )
    assert arr.status_code == 200 and arr.json()["status"] == "arrived"

    # 下转：由当前持有机构 county 发起 → current_org 切到下转目标 township
    assert client.post(
        f"/api/spd/referrals/{case_id}/down", json={"target_org_id": township["id"]}, headers=vdoc
    ).status_code == 403
    down = client.post(
        f"/api/spd/referrals/{case_id}/down", json={"target_org_id": township["id"]}, headers=cdoc
    )
    assert down.status_code == 200 and down.json()["current_org_id"] == township["id"]

    # 随访接收：当前机构已易主到 township → county 403、township 放行闭环
    assert client.post(
        f"/api/spd/referrals/{case_id}/receive-followup", json={}, headers=cdoc
    ).status_code == 403
    closed = client.post(
        f"/api/spd/referrals/{case_id}/receive-followup", json={}, headers=tdoc
    )
    assert closed.status_code == 200 and closed.json()["status"] == "closed"


def test_referral_legacy_station_reviewed_still_advances(client, h, base):
    """ADR-0005 存量兼容：收敛前停在 station_reviewed 的在途单仍可由其上级卫生院续走。

    还原真实存量单形态：current_org 锚在服务站机构（其 parent=卫生院），并用
    **机构账号**（卫生院医生）走 _assert_review_authority 的 parent 校验推进——
    不走全域角色旁路，钉住兼容项与越权校验的组合行为。
    """
    township_id = base["township"]["id"]
    # 收敛前遗留的服务站机构：村级、挂在卫生院之下
    station = client.post(
        "/api/organizations",
        json={"name": "存量服务站", "org_type": "village", "level": "village",
              "parent_id": township_id},
        headers=h,
    ).json()
    client.post(
        "/api/users",
        json={"username": "legacy_tdoc", "password": "pass123456", "role": "doctor",
              "org_id": township_id},
        headers=h,
    )
    tdoc = client.post(
        "/api/auth/login", json={"username": "legacy_tdoc", "password": "pass123456"}
    ).json()
    tdoc = {"Authorization": f"Bearer {tdoc['access_token']}"}

    patient = client.post(
        "/api/patients",
        json={"name": "存量链测试", "id_card": "330182198206060096", "phone": "13900009096"},
        headers=h,
    ).json()
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": base["village"]["id"], "risk_level": "high"},
        headers=h,
    )
    case_id = client.post(
        "/api/spd/referrals",
        json={"patient_id": patient["id"], "program_code": "hypertension", "reason": "上转"},
        headers=h,
    ).json()["id"]
    # 模拟收敛前的存量在途单：状态停在 station_reviewed、锚在服务站机构
    from app.database import SessionLocal
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        case = db.get(SpdReferralCase, case_id)
        case.status = "station_reviewed"
        case.current_org_id = station["id"]
        db.commit()
    resp = client.post(
        f"/api/spd/referrals/{case_id}/review", json={"action": "pass"}, headers=tdoc
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "township_reviewed", "存量 station_reviewed 应按旧链续走"


def test_closure_rate_excludes_rejected(client, h):
    stats = client.get("/api/spd/referrals-stats/closure", headers=h).json()
    assert stats["denominator"] == (
        stats["total"] - stats["by_status"].get("rejected", 0)
        - stats["by_status"].get("withdrawn", 0)
    )


def test_referral_rule_check(client, h, base):
    client.post(
        "/api/spd/referral-rules",
        json={"code": "rr_bp", "name": "血压极高需上转", "program_code": "hypertension",
              "conditions": [{"field": "bp_sys", "op": ">=", "value": 180,
                              "label": "收缩压≥180"}],
              "handle_level": "county"},
        headers=h,
    )
    patient = base["patients"][5]
    client.post(
        "/api/spd/measurements",
        json={"patient_id": patient["id"], "metric": "bp_sys", "value": 195,
              "unit": "mmHg"},
        headers=h,
    )
    resp = client.post(
        "/api/spd/referral-rules/check",
        json={"patient_id": patient["id"], "program_code": "hypertension"},
        headers=h,
    ).json()
    assert resp["triggered"] is True
    assert resp["hits"][0]["rule"]["code"] == "rr_bp"
    assert resp["case"] is None, "默认不自动开单"


def test_empty_rule_never_matches(client, h, base):
    """空规则不命中任何人——否则一次自动识别会把全县人口纳进目标池。"""
    from app.spd.rules import evaluate

    hit, matched = evaluate([], {"age": 70}, mode="all")
    assert hit is False and matched == []


# ============================================================ 生命周期


def test_death_cancels_open_work(client, h, base, team):
    patient = base["patients"][3]
    enrollment = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "copd",
              "org_id": base["township"]["id"], "team_id": team["id"]},
        headers=h,
    ).json()
    client.post(
        "/api/spd/tasks",
        json={"patient_id": patient["id"], "title": "待办随访",
              "task_type": "followup", "enrollment_id": enrollment["id"],
              "program_code": "copd"},
        headers=h,
    )
    resp = client.post(
        f"/api/spd/enrollments/{enrollment['id']}/lifecycle",
        json={"event": "death", "reason": "呼吸衰竭",
              "occurred_at": date.today().isoformat()},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrollment"]["status"] == "dead"
    assert body["closed"]["tasks"] >= 1, "死亡应终止未完成任务"

    resume = client.post(
        f"/api/spd/enrollments/{enrollment['id']}/lifecycle",
        json={"event": "resume"}, headers=h,
    )
    assert resume.status_code == 409, "已死亡档案不可恢复管理"


def test_cross_org_migration_needs_confirmation(client, h, base, team):
    patient = base["patients"][4]
    enrollment = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "ckd",
              "org_id": base["township"]["id"], "team_id": team["id"]},
        headers=h,
    ).json()
    resp = client.post(
        f"/api/spd/enrollments/{enrollment['id']}/lifecycle",
        json={"event": "migrate", "reason": "迁往县城",
              "target_org_id": base["county"]["id"]},
        headers=h,
    ).json()
    assert resp["pending_confirm"] is True
    assert resp["enrollment"]["status"] == "active", "未确认前仍在管，不能让患者悬空"

    confirmed = client.post(
        f"/api/spd/lifecycle-events/{resp['event_id']}/confirm", headers=h
    ).json()
    assert confirmed["enrollment"]["status"] == "migrated"


# ============================================================ 服务包


def test_package_binding_and_usage_limit(client, h, base, enrollment):
    package = client.post(
        "/api/spd/service-packages",
        json={"code": "pkg_hp", "name": "高血压基础包", "program_code": "hypertension",
              "price": 120, "period_days": 365,
              "items": [{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}]},
        headers=h,
    ).json()
    enrollment = client.get(
        "/api/spd/enrollments?program_code=hypertension", headers=h
    ).json()[0]
    binding = client.post(
        f"/api/spd/enrollments/{enrollment['id']}/packages",
        json={"package_id": package["id"]}, headers=h,
    )
    assert binding.status_code == 201, binding.text
    binding_id = binding.json()["id"]
    for _ in range(2):
        resp = client.post(
            f"/api/spd/package-bindings/{binding_id}/usages",
            json={"item_code": "bp_check", "qty": 1}, headers=h,
        )
        assert resp.status_code == 201
    over = client.post(
        f"/api/spd/package-bindings/{binding_id}/usages",
        json={"item_code": "bp_check", "qty": 1}, headers=h,
    )
    assert over.status_code == 409, "剩余次数不足应拒绝，不允许扣成负数"


# ============================================================ 考核与积分


def test_scoring_run_and_drilldown(client, h, base):
    plan = client.post(
        "/api/spd/assess-plans",
        json={"code": "plan_town", "name": "卫生院慢专病考核", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": "followup_rate", "weight": 60},
                        {"indicator_code": "referral_closure_rate", "weight": 40}]},
        headers=h,
    )
    assert plan.status_code == 201, plan.text
    period = date.today().strftime("%Y-%m")
    run = client.post(
        "/api/spd/scores/run",
        json={"plan_id": plan.json()["id"], "period": period,
              "object_ids": [base["township"]["id"]]},
        headers=h,
    )
    assert run.status_code == 200, run.text
    assert run.json()["scored"] == 1

    scores = client.get(
        f"/api/spd/scores?plan_id={plan.json()['id']}&period={period}", headers=h
    ).json()
    assert scores and scores[0]["rank"] == 1
    detail = client.get(f"/api/spd/scores/{scores[0]['id']}", headers=h).json()
    codes = {d.get("indicator_code") for d in detail["detail"]}
    assert {"followup_rate", "referral_closure_rate"} == codes
    assert all("metrics" in d for d in detail["detail"]), "应能下钻到原始数据"

    analysis = client.get(
        f"/api/spd/scores-analysis?plan_id={plan.json()['id']}&period={period}", headers=h
    ).json()
    assert analysis["total"] == 1
    assert "distribution" in analysis


def test_plan_rejects_unknown_indicator(client, h):
    resp = client.post(
        "/api/spd/assess-plans",
        json={"code": "plan_bad", "name": "坏方案", "level": "village",
              "items": [{"indicator_code": "not_exists", "weight": 100}]},
        headers=h,
    )
    assert resp.status_code == 422


def test_indicator_formula_validated(client, h):
    resp = client.post(
        "/api/spd/indicators",
        json={"code": "bad_ind", "name": "坏指标", "data_source": "task",
              "formula": "__import__('os').system('ls')"},
        headers=h,
    )
    assert resp.status_code == 422
    assert "公式非法" in resp.json()["detail"]


def test_points_awarded_and_redeem(client, h, base):
    account = client.get("/api/spd/point-accounts/me", headers=h).json()
    assert account["balance"] > 0, "签约与有效上转应已入账积分"

    goods = client.post(
        "/api/spd/goods",
        json={"code": "g_umbrella", "name": "雨伞", "points": 5, "stock": 1},
        headers=h,
    ).json()
    redeem = client.post("/api/spd/redeems", json={"goods_id": goods["id"]}, headers=h)
    assert redeem.status_code == 201, redeem.text
    code = redeem.json()["verify_code"]

    again = client.post("/api/spd/redeems", json={"goods_id": goods["id"]}, headers=h)
    assert again.status_code == 409, "库存耗尽应拒绝，不允许扣成负数"

    verified = client.post(
        "/api/spd/redeems/verify", json={"verify_code": code}, headers=h
    ).json()
    assert verified["status"] == "verified"
    repeat = client.post("/api/spd/redeems/verify", json={"verify_code": code}, headers=h)
    assert repeat.status_code == 404, "核销码不可重复核销"


def test_signin_once_per_day(client, h):
    first = client.post("/api/spd/point-accounts/signin", headers=h)
    assert first.status_code == 200
    second = client.post("/api/spd/point-accounts/signin", headers=h)
    assert second.status_code == 409


# ============================================================ 智能随访


def test_followup_plan_and_abnormal_escalation(client, h, base):
    patient = base["patients"][2]
    rules = client.get("/api/spd/followup-rules?scene=inpatient", headers=h).json()
    rule = next(r for r in rules if r["code"] == "fr_discharge")
    plan = client.post(
        "/api/spd/followup-plans",
        json={"patient_id": patient["id"], "rule_id": rule["id"],
              "base_date": date.today().isoformat(), "org_id": base["township"]["id"]},
        headers=h,
    )
    assert plan.status_code == 201, plan.text
    assert plan.json()["created"] == len(rule["points"])

    record = plan.json()["items"][0]
    context = client.get(
        f"/api/spd/followup-records/{record['id']}/context", headers=h
    ).json()
    assert context["patient"]["id"] == patient["id"]
    assert context["questionnaire"]["code"] == "q_discharge"

    before = len(client.get(
        f"/api/spd/tasks?patient_id={patient['id']}&task_type=report", headers=h
    ).json())
    executed = client.post(
        f"/api/spd/followup-records/{record['id']}/execute",
        json={"channel": "phone", "answers": {"pain": 9, "fever": "是", "medicine": "是"},
              "result": "疼痛明显"},
        headers=h,
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["abnormal_level"] == "high", "取命中的最高级别"
    after = client.get(
        f"/api/spd/tasks?patient_id={patient['id']}&task_type=report", headers=h
    ).json()
    assert len(after) == before + 1, "重度异常应自动派处置任务"


def test_unreachable_followup_is_distinct(client, h, base):
    patient = base["patients"][2]
    records = client.get(
        f"/api/spd/followup-records?patient_id={patient['id']}&status=planned", headers=h
    ).json()
    resp = client.post(
        f"/api/spd/followup-records/{records[0]['id']}/execute",
        json={"unreachable": True, "result": "三次拨打无人接听"}, headers=h,
    ).json()
    assert resp["status"] == "unreachable"
    stats = client.get("/api/spd/followup-stats", headers=h).json()
    assert stats["by_status"].get("unreachable", 0) >= 1


def test_call_task_and_qc(client, h, base):
    patient = base["patients"][2]
    call = client.post(
        "/api/spd/call-tasks",
        json={"patient_id": patient["id"], "ref_type": "followup"}, headers=h,
    )
    assert call.status_code == 201
    result = client.post(
        f"/api/spd/call-tasks/{call.json()['id']}/result",
        json={"status": "connected", "duration_s": 180,
              "record_url": "/records/1.mp3", "result": "已联系"},
        headers=h,
    ).json()
    assert result["duration_s"] == 180

    qc = client.post(
        "/api/spd/qc-samples/plan", json={"ratio": 1.0}, headers=h
    ).json()
    assert qc["planned"] >= 1
    samples = client.get(f"/api/spd/qc-samples?batch={qc['batch']}", headers=h).json()
    graded = client.post(
        f"/api/spd/qc-samples/{samples[0]['id']}/result",
        json={"result": "pass", "method": "record"}, headers=h,
    ).json()
    assert graded["result"] == "pass"


# ============================================================ 智能辅助报告


def test_report_generation(client, h, base):
    templates = client.get("/api/spd/report-templates", headers=h).json()
    assert {"rpt_daily", "rpt_weekly", "rpt_monthly"} <= {t["code"] for t in templates}
    resp = client.post(
        "/api/spd/report-instances",
        json={"template_code": "rpt_daily", "org_id": base["township"]["id"]},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    content = resp.json()["content"]
    keys = {s["key"] for s in content["sections"]}
    assert {"summary", "todo", "alert"} == keys
    summary = next(s for s in content["sections"] if s["key"] == "summary")
    assert "在管患者" in summary["text"]


def test_report_task_lifecycle(client, h):
    templates = client.get("/api/spd/report-templates", headers=h).json()
    task = client.post(
        "/api/spd/report-tasks",
        json={"template_id": templates[0]["id"], "name": "每日推送",
              "frequency": "daily", "push_time": "08:00"},
        headers=h,
    )
    assert task.status_code == 201
    paused = client.patch(
        f"/api/spd/report-tasks/{task.json()['id']}",
        json={"status": "paused"}, headers=h,
    ).json()
    assert paused["status"] == "paused"
    run = client.post(
        "/api/spd/report-instances", json={"task_id": task.json()["id"]}, headers=h
    )
    assert run.status_code == 201


# ============================================================ 各端工作台


@pytest.mark.parametrize("path", [
    "/api/spd/workbench/admin",
    "/api/spd/workbench/health-commission",
    "/api/spd/workbench/expert",
    "/api/spd/workbench/center",
    "/api/spd/workbench/team?role=member",
    "/api/spd/workbench/team?role=expert",
    "/api/spd/workbench/team?role=case_manager",
    "/api/spd/workbench/doctor-mobile",
    "/api/spd/stats/region",
    "/api/spd/catalog",
])
def test_workbenches_available(client, h, path):
    resp = client.get(path, headers=h)
    assert resp.status_code == 200, f"{path} → {resp.text}"
    assert isinstance(resp.json(), dict)


def test_admin_workbench_reports_parallel_tracks(client, h):
    wb = client.get("/api/spd/workbench/admin", headers=h).json()
    assert wb["parallel_tracks"]["chronic"]["programs"] >= 6
    assert wb["parallel_tracks"]["specialty"]["programs"] >= 2
    assert wb["config_health"]["programs"] >= 8


def test_overdue_sweep_marks_tasks(client, h, base):
    """到期未办的任务在工作台刷新时应被置为超期。"""
    patient = base["patients"][5]
    task = client.post(
        "/api/spd/tasks",
        json={"patient_id": patient["id"], "title": "昨天就该办的任务",
              "task_type": "followup", "due_days": 0},
        headers=h,
    ).json()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    summary = client.get(f"/api/spd/tasks/summary?today={tomorrow}", headers=h).json()
    assert summary["swept"]["overdue"] >= 1
    refreshed = client.get(f"/api/spd/tasks/{task['id']}", headers=h).json()
    assert refreshed["status"] == "overdue"


def test_workload_stats(client, h):
    workload = client.get("/api/spd/workload?object_type=org", headers=h).json()
    assert "items" in workload
    assert workload["period"]


def test_migration_covers_every_spd_table():
    """防回退：新增 spd_* 表必须同步写进迁移，否则生产库升不上去。

    启动时的 `Base.metadata.create_all` 会让开发与演示环境"看起来正常"，
    而生产走 alembic——漏写迁移的表要等到上线才发现。
    """
    import pathlib

    from app.database import Base

    migration = next(
        p for p in (pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions")
        .iterdir() if p.name.startswith("d1a2b3c4e5f6")
    ).read_text(encoding="utf-8")
    missing = [
        name for name in Base.metadata.tables
        if name.startswith("spd_") and f'"{name}"' not in migration
    ]
    assert missing == [], f"以下慢专病表没有写进迁移：{missing}"
