"""慢专病随访域（`spd/followup`）30 个端点的**响应契约**特征化网。

场景全部**经 HTTP API 种出来**，对代表性端点断言完整精确 JSON（dict 相等）
与键序（`list(resp.json().keys())`），为补 `response_model` 提供逐字节基线
（治理不得改响应字节，CLAUDE.md 第 7 条）。范式照 `test_spd_assess_contract.py`。

四处最要紧的判断（加契约前后都得成立）：

1. **`/followup-records/{id}/execute` 的 `action` 是条件键**：失访分支 19 个键
   **没有** `action`（不是 null），正常执行分支在**末尾**追加第 20 个键——
   契约用「`action` 声明在最后 + `exclude_unset`」对齐，两条分支各钉一条。
2. **`/followup-plans/auto-match` 两条分支键集不同**：无可用方案分支是
   `matched, created, note`，扫描分支是 `scanned, matched, created`——
   一个模型排不出两种键集，契约是二选一联合（无方案分支 `extra="forbid"`）。
3. **`executed_at`/`valid_from`/`last_run_at` 未发生时是空串不是 null**
   （`String(10)` 默认 "" / `isoformat() if ... else ""`），两种取值都要钉。
4. 本模块**没有 Money/Float 列**，数值只有 Integer（裸 int）与
   `round(x/y*100, 1)` 派生的 float（`completion_rate`，0 也是 `0.0`）。
"""
import re
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
def auth(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/spd"
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _ts(value):
    """时间戳只钉格式不钉取值——它是本场景里唯一不可复现的字节。"""
    assert isinstance(value, str) and ISO_TS.match(value), value
    return value


RULE_KEYS = ["id", "code", "name", "scene", "dept", "program_code", "diagnosis_keywords",
             "surgery_keywords", "order_keywords", "points", "questionnaire_code",
             "executor_role", "allow_depts", "allow_roles", "preset", "active"]
Q_KEYS = ["id", "code", "name", "scene", "items", "abnormal_rules", "track_dept",
          "handle_role", "preset", "active"]
RECORD_KEYS = ["id", "patient_id", "patient_name", "program_code", "rule_id",
               "questionnaire_code", "scene", "org_id", "dept", "planned_at",
               "executed_at", "channel", "executor_id", "answers", "abnormal_level",
               "result", "evidence", "status", "created_at"]
TEMPLATE_KEYS = ["id", "code", "name", "period", "scope_level", "sections",
                 "variables", "active"]
RTASK_KEYS = ["id", "template_id", "name", "frequency", "push_time", "subscriber_ids",
              "org_ids", "valid_from", "valid_to", "priority", "status", "last_run_at"]


def _record(world, rid, patient_name="", **overrides):
    """按当前世界推出某条随访记录的完整精确出参（created_at 由调用处代入）。"""
    base = {
        "id": rid, "patient_id": world["patient"]["id"], "patient_name": patient_name,
        "program_code": "", "rule_id": world["rule"]["id"],
        "questionnaire_code": "ct_fu_q", "scene": "inpatient",
        "org_id": world["org"]["id"], "dept": "内科", "planned_at": "",
        "executed_at": "", "channel": "phone", "executor_id": None, "answers": {},
        "abnormal_level": "none", "result": "", "evidence": [], "status": "planned",
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def world(client, auth):
    """一家机构 × 一名患者 × 两批随访计划（执行出 done/unreachable/改期三态）
    + 呼叫回写 + 抽查 + 报告链路 + 第二名患者供自动匹配。

    顺序敏感的步骤在这里一次做完并留底（如 last_run_at 空串分支要在生成报告前
    取样），测试函数只对留底与终态断言。
    """
    today = date.today()
    d = {"today": today.isoformat(),
         "yesterday": (today - timedelta(days=1)).isoformat(),
         "t_minus2": (today - timedelta(days=2)).isoformat(),
         "t_plus5": (today + timedelta(days=3)).isoformat(),
         "t_plus7": (today + timedelta(days=7)).isoformat(),
         "tomorrow": (today + timedelta(days=1)).isoformat()}
    # 第二批计划基准日 today-2，点位 [0,7] → today-2 与 today+5
    d["t_plus5"] = (today + timedelta(days=5)).isoformat()

    org = client.post(
        "/api/organizations",
        json={"name": "契约随访卫生院", "org_type": "township", "level": "township"},
        headers=auth,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约随访患者", "id_card": "330666199006150011", "gender": "男",
              "birth_date": "1990-06-15", "phone": "13900001111"},
        headers=auth,
    ).json()
    patient2 = client.post(
        "/api/patients",
        json={"name": "契约匹配患者", "id_card": "330666199006150022", "gender": "女",
              "birth_date": "1990-06-15", "phone": "13900002222"},
        headers=auth,
    ).json()
    # 患者一的就诊史：门诊（不含匹配关键词）+ 住院登记（自动生成 inpatient 就诊）
    enc1 = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "doctor_name": "王医生",
              "encounter_type": "outpatient", "diagnosis_name": "上呼吸道感染"},
        headers=auth,
    )
    assert enc1.status_code == 201, enc1.text
    ward = client.post("/api/inpatient/wards", json={"org_id": org["id"], "name": "契约病区"},
                       headers=auth).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "01"},
                      headers=auth).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "李医生", "diagnosis_name": "阑尾炎术后"},
        headers=auth,
    )
    assert admission.status_code == 201, admission.text
    # 患者二的门诊：命中自动匹配关键词"高血压"
    client.post(
        "/api/encounters",
        json={"patient_id": patient2["id"], "org_id": org["id"],
              "encounter_type": "outpatient", "diagnosis_name": "原发性高血压"},
        headers=auth,
    )

    questionnaire = client.post(
        f"{B}/questionnaires",
        json={"code": "ct_fu_q", "name": "契约出院问卷", "scene": "inpatient",
              "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}],
              "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7},
                                  "level": "high", "action": "通知主管医师"}],
              "track_dept": "内科", "handle_role": "doctor"},
        headers=auth,
    )
    assert questionnaire.status_code == 201, questionnaire.text
    rule = client.post(
        f"{B}/followup-rules",
        json={"code": "ct_fu_rule", "name": "契约出院随访", "scene": "inpatient",
              "dept": "内科", "diagnosis_keywords": ["冠心病"], "points": [0, 7],
              "questionnaire_code": "ct_fu_q", "executor_role": "nurse"},
        headers=auth,
    )
    assert rule.status_code == 201, rule.text
    rule_patched = client.patch(
        f"{B}/followup-rules/{rule.json()['id']}",
        json={"name": "契约出院随访v2"}, headers=auth,
    )
    client.patch(f"{B}/followup-rules/{rule.json()['id']}",
                 json={"name": "契约出院随访"}, headers=auth)
    world = {"org": org, "patient": patient, "patient2": patient2,
             "rule": rule.json(), "rule_keys": list(rule.json().keys()),
             "rule_patched": rule_patched.json(),
             "q": questionnaire.json(), "q_keys": list(questionnaire.json().keys()),
             **{f"d_{k}": v for k, v in d.items()}}

    plan1 = client.post(
        f"{B}/followup-plans",
        json={"patient_id": patient["id"], "rule_id": world["rule"]["id"],
              "base_date": d["today"], "org_id": org["id"]},
        headers=auth,
    )
    assert plan1.status_code == 201, plan1.text
    world["plan1"] = plan1.json()
    rec_a, rec_b = [item["id"] for item in plan1.json()["items"]]

    executed = client.post(
        f"{B}/followup-records/{rec_a}/execute",
        json={"answers": {"pain": 8}, "channel": "phone", "result": "疼痛明显",
              "evidence": ["rec-001.mp3"]},
        headers=auth,
    )
    assert executed.status_code == 200, executed.text
    world["executed"] = executed.json()
    world["executed_keys"] = list(executed.json().keys())

    plan2 = client.post(
        f"{B}/followup-plans",
        json={"patient_id": patient["id"], "rule_id": world["rule"]["id"],
              "base_date": d["t_minus2"], "org_id": org["id"]},
        headers=auth,
    ).json()
    rec_c, rec_d = [item["id"] for item in plan2["items"]]
    unreachable = client.post(
        f"{B}/followup-records/{rec_c}/execute",
        json={"unreachable": True, "result": "三次未接听"}, headers=auth,
    )
    assert unreachable.status_code == 200, unreachable.text
    world["unreachable"] = unreachable.json()
    world["unreachable_keys"] = list(unreachable.json().keys())

    patched = client.patch(
        f"{B}/followup-records/{rec_b}", json={"planned_at": d["yesterday"]}, headers=auth
    )
    assert patched.status_code == 200, patched.text
    world["patched"] = patched.json()
    world.update({"rec_a": rec_a, "rec_b": rec_b, "rec_c": rec_c, "rec_d": rec_d})

    # 呼叫任务：不传手机号回落到患者档案号码；接通结果回写记录 B
    call = client.post(
        f"{B}/call-tasks",
        json={"patient_id": patient["id"], "ref_type": "followup", "ref_id": rec_b},
        headers=auth,
    )
    assert call.status_code == 201, call.text
    world["call"] = call.json()
    world["call_keys"] = list(call.json().keys())
    call_result = client.post(
        f"{B}/call-tasks/{call.json()['id']}/result",
        json={"status": "connected", "duration_s": 65,
              "record_url": "http://cdn/rec-b.mp3", "result": "已接通"},
        headers=auth,
    )
    world["call_result"] = call_result.json()

    # 抽查：done 只有 A → 全抽
    qc_plan = client.post(f"{B}/qc-samples/plan",
                          json={"ratio": 1, "batch": "QCT1"}, headers=auth)
    assert qc_plan.status_code == 200, qc_plan.text
    world["qc_plan"] = qc_plan.json()
    sample = client.get(f"{B}/qc-samples", params={"batch": "QCT1"}, headers=auth).json()[0]
    world["qc_result"] = client.post(
        f"{B}/qc-samples/{sample['id']}/result",
        json={"result": "pass", "method": "record", "note": "记录完整"}, headers=auth,
    ).json()

    # 报告链路：模板 → 推送任务（last_run_at 空串分支先取样）→ 生成实例
    template = client.post(
        f"{B}/report-templates",
        json={"code": "ct_rpt", "name": "契约日报", "period": "daily",
              "scope_level": "center",
              "sections": [{"key": "summary", "title": "总体概览", "type": "text"}],
              "variables": {"foo": "bar"}},
        headers=auth,
    )
    assert template.status_code == 201, template.text
    world["template"] = template.json()
    world["template_keys"] = list(template.json().keys())
    world["template_patched"] = client.patch(
        f"{B}/report-templates/{template.json()['id']}",
        json={"variables": {"foo": "baz"}}, headers=auth,
    ).json()
    rtask = client.post(
        f"{B}/report-tasks",
        json={"template_id": template.json()["id"], "name": "契约晨报",
              "frequency": "daily", "push_time": "08:00", "subscriber_ids": [1],
              "org_ids": [org["id"]], "priority": 2},
        headers=auth,
    )
    assert rtask.status_code == 201, rtask.text
    world["rtask"] = rtask.json()
    world["rtask_keys"] = list(rtask.json().keys())
    world["rtask_patched"] = client.patch(
        f"{B}/report-tasks/{rtask.json()['id']}", json={"priority": 3}, headers=auth
    ).json()
    instance = client.post(
        f"{B}/report-instances",
        json={"task_id": rtask.json()["id"], "org_id": org["id"]},
        headers=auth,
    )
    assert instance.status_code == 201, instance.text
    world["instance"] = instance.json()
    world["instance_keys"] = list(instance.json().keys())

    # 健康日历的复诊来源
    revisit = client.post(
        f"{B}/revisits",
        json={"patient_id": patient["id"], "plan_date": d["today"], "dept": "内科",
              "items": "复查血压"},
        headers=auth,
    )
    assert revisit.status_code == 201, revisit.text
    world["revisit"] = revisit.json()

    # 自动匹配：无可用方案分支（checkup 场景没有配关键词的方案）先取样
    world["auto_empty"] = client.post(
        f"{B}/followup-plans/auto-match",
        json={"scene": "checkup", "org_id": org["id"]}, headers=auth,
    ).json()
    world["auto_empty_keys"] = list(world["auto_empty"].keys())
    # 扫描分支：门诊场景，患者二命中"高血压"
    rule2 = client.post(
        f"{B}/followup-rules",
        json={"code": "ct_fu_auto", "name": "契约门诊自动随访", "scene": "outpatient",
              "diagnosis_keywords": ["高血压"], "points": [30]},
        headers=auth,
    ).json()
    world["rule2"] = rule2
    auto = client.post(
        f"{B}/followup-plans/auto-match",
        json={"scene": "outpatient", "org_id": org["id"]}, headers=auth,
    )
    assert auto.status_code == 200, auto.text
    world["auto"] = auto.json()
    world["auto_keys"] = list(auto.json().keys())
    return world


# ============================================================ 方案与问卷


def test_方案新建回执完整精确(client, auth, world):
    created = world["rule"]
    assert world["rule_keys"] == RULE_KEYS
    assert created == {
        "id": created["id"], "code": "ct_fu_rule", "name": "契约出院随访",
        "scene": "inpatient", "dept": "内科", "program_code": "",
        "diagnosis_keywords": ["冠心病"], "surgery_keywords": [], "order_keywords": [],
        "points": [0, 7], "questionnaire_code": "ct_fu_q", "executor_role": "nurse",
        "allow_depts": [], "allow_roles": [], "preset": False, "active": True,
    }


def test_方案列表行与修改回执同形(client, auth, world):
    rows = client.get(f"{B}/followup-rules", headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_fu_rule")
    assert list(mine.keys()) == RULE_KEYS
    assert mine == world["rule"]
    assert world["rule_patched"] == {**world["rule"], "name": "契约出院随访v2"}


def test_问卷新建列表修改(client, auth, world):
    created = world["q"]
    assert world["q_keys"] == Q_KEYS
    assert created == {
        "id": created["id"], "code": "ct_fu_q", "name": "契约出院问卷",
        "scene": "inpatient",
        "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7},
                            "level": "high", "action": "通知主管医师"}],
        "track_dept": "内科", "handle_role": "doctor", "preset": False, "active": True,
    }
    rows = client.get(f"{B}/questionnaires", params={"scene": "inpatient"},
                      headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_fu_q")
    assert list(mine.keys()) == Q_KEYS and mine == created

    patched = client.patch(f"{B}/questionnaires/{created['id']}",
                           json={"track_dept": "心内科"}, headers=auth)
    assert patched.status_code == 200
    assert patched.json() == {**created, "track_dept": "心内科"}
    client.patch(f"{B}/questionnaires/{created['id']}",
                 json={"track_dept": "内科"}, headers=auth)


# ============================================================ 计划生成与执行


def test_生成计划回执完整精确(client, auth, world):
    body = world["plan1"]
    assert list(body.keys()) == ["created", "items"]
    assert [list(item.keys()) for item in body["items"]] == [RECORD_KEYS] * 2
    a, b = body["items"]
    assert body == {
        "created": 2,
        "items": [
            _record(world, world["rec_a"], planned_at=world["d_today"],
                    created_at=_ts(a["created_at"])),
            _record(world, world["rec_b"], planned_at=world["d_t_plus7"],
                    created_at=_ts(b["created_at"])),
        ],
    }


def test_执行回执_正常分支末尾多action键(client, auth, world):
    body = world["executed"]
    assert world["executed_keys"] == RECORD_KEYS + ["action"]
    assert body == {
        **_record(world, world["rec_a"], planned_at=world["d_today"],
                  executed_at=world["d_today"], executor_id=1,
                  answers={"pain": 8}, abnormal_level="high", result="疼痛明显",
                  evidence=["rec-001.mp3"], status="done",
                  created_at=_ts(body["created_at"])),
        "action": "通知主管医师",
    }


def test_执行回执_失访分支没有action键(client, auth, world):
    body = world["unreachable"]
    assert world["unreachable_keys"] == RECORD_KEYS  # 19 键，action 整个不出现
    assert body == _record(
        world, world["rec_c"], planned_at=world["d_t_minus2"],
        executed_at=world["d_today"], executor_id=1, result="三次未接听",
        status="unreachable", created_at=_ts(body["created_at"]),
    )


def test_改期回执完整精确(client, auth, world):
    body = world["patched"]
    assert list(body.keys()) == RECORD_KEYS
    assert body == _record(world, world["rec_b"], planned_at=world["d_yesterday"],
                           created_at=_ts(body["created_at"]))


def test_看板列表按计划日排序且带患者名(client, auth, world):
    rows = client.get(f"{B}/followup-records",
                      params={"patient_id": world["patient"]["id"]}, headers=auth).json()
    assert [list(r.keys()) for r in rows] == [RECORD_KEYS] * 4
    assert [r["id"] for r in rows] == [world["rec_c"], world["rec_b"],
                                       world["rec_a"], world["rec_d"]]
    # 记录 B 被呼叫结果回写过：result 追加、evidence 收录音地址
    assert rows[1] == _record(
        world, world["rec_b"], patient_name="契约随访患者",
        planned_at=world["d_yesterday"], result="已接通",
        evidence=["http://cdn/rec-b.mp3"], created_at=_ts(rows[1]["created_at"]),
    )
    assert rows[3] == _record(world, world["rec_d"], patient_name="契约随访患者",
                              planned_at=world["d_t_plus5"],
                              created_at=_ts(rows[3]["created_at"]))


def test_随访前置资料聚合完整精确(client, auth, world):
    body = client.get(f"{B}/followup-records/{world['rec_a']}/context",
                      headers=auth).json()
    assert list(body.keys()) == ["record", "patient", "encounters", "admissions",
                                 "history", "questionnaire"]
    encs = body["encounters"]
    adms = body["admissions"]
    assert body == {
        "record": {
            **_record(world, world["rec_a"], patient_name="契约随访患者",
                      planned_at=world["d_today"], executed_at=world["d_today"],
                      executor_id=1, answers={"pain": 8}, abnormal_level="high",
                      result="疼痛明显", evidence=["rec-001.mp3"], status="done",
                      created_at=_ts(body["record"]["created_at"])),
        },
        "patient": {"id": world["patient"]["id"], "name": "契约随访患者",
                    "gender": "男", "birth_date": "1990-06-15", "phone": "13900001111"},
        "encounters": [
            {"id": encs[0]["id"], "encounter_type": "inpatient",
             "diagnosis_name": "阑尾炎术后", "doctor_name": "李医生",
             "created_at": _ts(encs[0]["created_at"])},
            {"id": encs[1]["id"], "encounter_type": "outpatient",
             "diagnosis_name": "上呼吸道感染", "doctor_name": "王医生",
             "created_at": _ts(encs[1]["created_at"])},
        ],
        "admissions": [
            {"id": adms[0]["id"], "admitted_at": _ts(adms[0]["admitted_at"]),
             "discharged_at": "", "diagnosis_name": "阑尾炎术后",
             "doctor_name": "李医生", "status": "admitted"},
        ],
        # 历史随访只收 done，且**不带患者名**（patient_name 为空串）
        "history": [
            _record(world, world["rec_a"], planned_at=world["d_today"],
                    executed_at=world["d_today"], executor_id=1, answers={"pain": 8},
                    abnormal_level="high", result="疼痛明显", evidence=["rec-001.mp3"],
                    status="done", created_at=_ts(body["history"][0]["created_at"])),
        ],
        "questionnaire": world["q"],
    }


def test_自动匹配两条分支键集不同(client, auth, world):
    """无可用方案分支连 scanned 键都没有、多一个 note——这不是笔误，是当前字节。"""
    assert world["auto_empty_keys"] == ["matched", "created", "note"]
    assert world["auto_empty"] == {"matched": 0, "created": 0,
                                   "note": "没有配置了诊断关键词的可用方案"}
    assert world["auto_keys"] == ["scanned", "matched", "created"]
    # 机构 7 日内就诊 3 条（患者一门诊+住院、患者二门诊），只有患者二命中
    assert world["auto"] == {"scanned": 3, "matched": 1, "created": 1}


def test_随访统计完整精确(client, auth, world):
    body = client.get(f"{B}/followup-stats", params={"dept": "内科"}, headers=auth).json()
    assert list(body.keys()) == ["total", "done", "completion_rate", "overdue",
                                 "by_status", "by_abnormal", "by_channel", "by_executor"]
    assert body == {
        "total": 4, "done": 1, "completion_rate": 25.0, "overdue": 1,
        "by_status": {"done": 1, "planned": 2, "unreachable": 1},
        "by_abnormal": {"high": 1},
        "by_channel": {"phone": 1},
        "by_executor": [{"executor_id": 1, "executor_name": "平台管理员", "done": 1}],
    }
    assert isinstance(body["completion_rate"], float)


# ============================================================ 呼叫与抽查


def test_呼叫任务创建与结果回执(client, auth, world):
    created = world["call"]
    assert world["call_keys"] == ["id", "phone", "status", "dispatch"]
    assert created == {"id": created["id"], "phone": "13900001111", "status": "pending",
                       "dispatch": {"accepted": True, "note": "待人工外呼"}}
    assert list(created["dispatch"].keys()) == ["accepted", "note"]

    assert list(world["call_result"].keys()) == ["id", "status", "duration_s"]
    assert world["call_result"] == {"id": created["id"], "status": "connected",
                                    "duration_s": 65}


def test_呼叫任务列表行完整精确(client, auth, world):
    rows = client.get(f"{B}/call-tasks", headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "patient_id", "patient_name", "phone", "ref_type", "ref_id", "status",
         "duration_s", "record_url", "result", "created_at"]
    ]
    assert rows[0] == {
        "id": world["call"]["id"], "patient_id": world["patient"]["id"],
        "patient_name": "契约随访患者", "phone": "13900001111", "ref_type": "followup",
        "ref_id": world["rec_b"], "status": "connected", "duration_s": 65,
        "record_url": "http://cdn/rec-b.mp3", "result": "已接通",
        "created_at": _ts(rows[0]["created_at"]),
    }


def test_抽查计划与结果回执(client, auth, world):
    assert list(world["qc_plan"].keys()) == ["batch", "pool", "planned", "created"]
    assert world["qc_plan"] == {"batch": "QCT1", "pool": 1, "planned": 1, "created": 1}
    assert list(world["qc_result"].keys()) == ["id", "result"]
    assert world["qc_result"] == {"id": world["qc_result"]["id"], "result": "pass"}


def test_抽查列表行嵌套随访记录(client, auth, world):
    rows = client.get(f"{B}/qc-samples", params={"batch": "QCT1"}, headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "record_id", "batch", "dept", "result", "method", "note", "record",
         "created_at"]
    ]
    assert rows[0] == {
        "id": world["qc_result"]["id"], "record_id": world["rec_a"], "batch": "QCT1",
        "dept": "内科", "result": "pass", "method": "record", "note": "记录完整",
        "record": _record(world, world["rec_a"], planned_at=world["d_today"],
                          executed_at=world["d_today"], executor_id=1,
                          answers={"pain": 8}, abnormal_level="high", result="疼痛明显",
                          evidence=["rec-001.mp3"], status="done",
                          created_at=_ts(rows[0]["record"]["created_at"])),
        "created_at": _ts(rows[0]["created_at"]),
    }


# ============================================================ 报告模板与推送


def test_报告模板新建列表修改(client, auth, world):
    created = world["template"]
    assert world["template_keys"] == TEMPLATE_KEYS
    assert created == {
        "id": created["id"], "code": "ct_rpt", "name": "契约日报", "period": "daily",
        "scope_level": "center",
        "sections": [{"key": "summary", "title": "总体概览", "type": "text"}],
        "variables": {"foo": "bar"}, "active": True,
    }
    assert world["template_patched"] == {**created, "variables": {"foo": "baz"}}
    rows = client.get(f"{B}/report-templates", params={"period": "daily"},
                      headers=auth).json()
    mine = next(r for r in rows if r["code"] == "ct_rpt")
    assert list(mine.keys()) == TEMPLATE_KEYS and mine == world["template_patched"]


def test_推送任务新建修改与last_run_at两种取值(client, auth, world):
    created = world["rtask"]
    assert world["rtask_keys"] == RTASK_KEYS
    assert created == {
        "id": created["id"], "template_id": world["template"]["id"], "name": "契约晨报",
        "frequency": "daily", "push_time": "08:00", "subscriber_ids": [1],
        "org_ids": [world["org"]["id"]], "valid_from": "", "valid_to": "",
        "priority": 2, "status": "active", "last_run_at": "",  # 未跑过是空串不是 null
    }
    assert world["rtask_patched"] == {**created, "priority": 3}

    rows = client.get(f"{B}/report-tasks", headers=auth).json()
    assert [list(r.keys()) for r in rows] == [RTASK_KEYS]
    # 生成报告后 last_run_at 回填 ISO 串——同一字段的另一种取值
    assert rows[0] == {**created, "priority": 3,
                       "last_run_at": _ts(rows[0]["last_run_at"])}


def test_生成报告回执完整精确(client, auth, world):
    body = world["instance"]
    assert world["instance_keys"] == ["id", "title", "period_label", "content"]
    assert body == {
        "id": body["id"], "title": f"契约日报（{world['d_today']}）",
        "period_label": world["d_today"],
        "content": {
            "period_label": world["d_today"],
            "sections": [{"key": "summary", "title": "总体概览", "type": "text",
                          "text": "在管患者 0 人，待办任务 1 条，其中超期 0 条。",
                          "metrics": {"enrolled": 0, "open_tasks": 1, "overdue": 0}}],
        },
    }


def test_报告实例列表与详情(client, auth, world):
    rows = client.get(f"{B}/report-instances", params={"template_code": "ct_rpt"},
                      headers=auth).json()
    assert [list(r.keys()) for r in rows] == [
        ["id", "title", "template_code", "period_label", "scope_level", "org_id",
         "created_at"]
    ]
    assert rows[0] == {
        "id": world["instance"]["id"], "title": world["instance"]["title"],
        "template_code": "ct_rpt", "period_label": world["d_today"],
        "scope_level": "center", "org_id": world["org"]["id"],
        "created_at": _ts(rows[0]["created_at"]),
    }
    detail = client.get(f"{B}/report-instances/{world['instance']['id']}",
                        headers=auth).json()
    assert list(detail.keys()) == ["id", "title", "template_code", "period_label",
                                   "scope_level", "org_id", "content", "subscriber_ids",
                                   "created_at"]
    assert detail == {
        "id": world["instance"]["id"], "title": world["instance"]["title"],
        "template_code": "ct_rpt", "period_label": world["d_today"],
        "scope_level": "center", "org_id": world["org"]["id"],
        "content": world["instance"]["content"], "subscriber_ids": [1],
        "created_at": _ts(detail["created_at"]),
    }


def test_健康日历两天两种填充(client, auth, world):
    today = client.get(
        f"{B}/health-calendar",
        params={"patient_id": world["patient"]["id"], "day": world["d_today"]},
        headers=auth,
    ).json()
    assert list(today.keys()) == ["day", "followups", "revisits", "tasks"]
    assert today == {
        "day": world["d_today"],
        "followups": [
            _record(world, world["rec_a"], planned_at=world["d_today"],
                    executed_at=world["d_today"], executor_id=1, answers={"pain": 8},
                    abnormal_level="high", result="疼痛明显", evidence=["rec-001.mp3"],
                    status="done", created_at=_ts(today["followups"][0]["created_at"])),
        ],
        "revisits": [{"id": world["revisit"]["id"], "plan_date": world["d_today"],
                      "dept": "内科", "items": "复查血压", "status": "planned"}],
        "tasks": [],
    }
    # 重度异常自动派的处置任务落在次日
    tomorrow = client.get(
        f"{B}/health-calendar",
        params={"patient_id": world["patient"]["id"], "day": world["d_tomorrow"]},
        headers=auth,
    ).json()
    assert tomorrow["followups"] == [] and tomorrow["revisits"] == []
    assert [list(t.keys()) for t in tomorrow["tasks"]] == [
        ["id", "title", "task_type", "status"]
    ]
    assert tomorrow["tasks"][0] == {"id": tomorrow["tasks"][0]["id"],
                                    "title": "随访异常处置：通知主管医师",
                                    "task_type": "report", "status": "pending"}


# ============================================================ 错误体


def test_各类错误体都只有detail(client, auth, world):
    cases = [
        (client.post(f"{B}/followup-rules",
                     json={"code": "ct_e1", "name": "无时点", "points": []},
                     headers=auth), 422),
        (client.post(f"{B}/followup-rules",
                     json={"code": "ct_e2", "name": "超范围", "points": [4000]},
                     headers=auth), 422),
        (client.post(f"{B}/followup-rules",
                     json={"code": "ct_fu_rule", "name": "重复", "points": [1]},
                     headers=auth), 409),
        (client.patch(f"{B}/followup-rules/999999", json={"name": "x"}, headers=auth), 404),
        (client.post(f"{B}/questionnaires",
                     json={"code": "ct_e3", "name": "坏规则",
                           "abnormal_rules": [{"when": {"field": "", "op": ">="}}]},
                     headers=auth), 422),
        (client.post(f"{B}/questionnaires",
                     json={"code": "ct_fu_q", "name": "重复"}, headers=auth), 409),
        (client.patch(f"{B}/questionnaires/999999", json={"name": "x"}, headers=auth), 404),
        (client.post(f"{B}/followup-plans",
                     json={"patient_id": world["patient"]["id"], "rule_id": 999999},
                     headers=auth), 404),
        (client.post(f"{B}/followup-records/999999/execute", json={}, headers=auth), 404),
        (client.post(f"{B}/followup-records/{world['rec_a']}/execute",
                     json={}, headers=auth), 409),  # 已完成
        (client.patch(f"{B}/followup-records/999999", json={}, headers=auth), 404),
        (client.patch(f"{B}/followup-records/{world['rec_a']}",
                      json={"status": "removed"}, headers=auth), 409),
        (client.post(f"{B}/call-tasks/999999/result",
                     json={"status": "connected"}, headers=auth), 404),
        (client.post(f"{B}/qc-samples/999999/result",
                     json={"result": "pass"}, headers=auth), 404),
        (client.post(f"{B}/report-templates",
                     json={"code": "ct_e4", "name": "空模板", "sections": []},
                     headers=auth), 422),
        (client.post(f"{B}/report-templates",
                     json={"code": "ct_rpt", "name": "重复",
                           "sections": [{"key": "summary"}]}, headers=auth), 409),
        (client.patch(f"{B}/report-templates/999999", json={"name": "x"},
                      headers=auth), 404),
        (client.post(f"{B}/report-tasks",
                     json={"template_id": 999999, "name": "孤儿任务"}, headers=auth), 404),
        (client.patch(f"{B}/report-tasks/999999", json={"name": "x"}, headers=auth), 404),
        (client.delete(f"{B}/report-tasks/999999", headers=auth), 404),
        (client.post(f"{B}/report-instances", json={"task_id": 999999}, headers=auth), 404),
        (client.post(f"{B}/report-instances", json={"template_code": "no_such"},
                     headers=auth), 404),
        (client.get(f"{B}/report-instances/999999", headers=auth), 404),
        (client.get(f"{B}/followup-records/999999/context", headers=auth), 404),
    ]
    for resp, expected in cases:
        assert resp.status_code == expected, f"{resp.request.url} -> {resp.text}"
        assert set(resp.json()) == {"detail"}


def test_删除推送任务204无响应体(client, auth, world):
    """该端点已由 status_code=204 声明契约（本批不动它），钉住无响应体这一事实。"""
    tmp = client.post(
        f"{B}/report-tasks",
        json={"template_id": world["template"]["id"], "name": "契约临时推送"},
        headers=auth,
    ).json()
    resp = client.delete(f"{B}/report-tasks/{tmp['id']}", headers=auth)
    assert resp.status_code == 204
    assert resp.content == b""
