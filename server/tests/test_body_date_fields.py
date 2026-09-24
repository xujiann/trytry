"""请求体日期字段的逐端点回归（P1-61）。

D-3 收敛的是"写了日期正则的 22 处"；另有 22 个请求体日期字段从来没写过正则，只是裸 `str`
（或 `min_length=10, max_length=10`），`2026/09/24`、`2026-02-31`、`abcdefghij`（恰好 10 个字符）
原样入库。`test_datestr_single_source.py` 的棘轮盯"还剩多少"，这里逐个端点钉住"改了的确实挡住了"。

请求体校验先于业务查找：id 填不存在的也能证明 422 来自日期字段；反过来给合法日期时，
同一请求必须**越过这个字段的校验**（落到 404/403/201 或别的字段上都行）——
证明换上真源没把合法输入也挡掉。

逐模块接一批、加一批行。
"""
from __future__ import annotations

import pytest

#: (路径, 其余字段, 日期字段, 是否可空)。其余字段只求过得了请求体校验，id 一律填不存在的。
#: 路径可带方法前缀（`PATCH /api/...`），不带即 POST。
CASES = [
    ("/api/analytics/outbound-visits",
     {"patient_id": 999999, "external_org_name": "市一院"}, "visit_date", False),
    ("/api/followups",
     {"patient_id": 999999, "org_id": 999999, "category": "chronic"}, "due_date", False),
    ("/api/inpatient/handovers", {"ward_id": 999999, "shift": "day"}, "handover_date", False),
    ("/api/surgery/requests/999999/schedule",
     {"room_id": 999999, "start_time": "09:00", "end_time": "10:00"}, "scheduled_date", False),
    ("/api/surgery/requests",
     {"admission_id": 999999, "surgery_name": "阑尾切除术"}, "planned_date", True),
    # 中心类型填不存在的：合法日期越过这一道后在业务校验上 422（字符串 detail），不落库
    ("/api/mgmt/qc", {"center_type": "no-such-center", "item": "质控项", "result": "pass"},
     "record_date", True),
    ("/api/mgmt/employees/999999/changes", {"change_type": "hire"}, "effective_date", True),
    ("/api/eldercare/assessments", {"patient_id": 999999, "adl_score": 100}, "assessed_date", True),
    ("/api/homevisits", {"patient_id": 999999, "org_id": 999999, "service_type": "nursing"},
     "expect_date", True),
    ("/api/knowledge", {"category": "regulation", "title": "日期回归条目"}, "expire_date", True),
    ("PATCH /api/knowledge/999999", {}, "expire_date", True),  # 空串 = 改为长期有效
    ("/api/materials/consumables", {"barcode": "DATE-REG-1", "name": "穿刺器", "org_id": 999999},
     "expire_date", True),
    ("/api/maternal/records/999999/visits", {"visit_type": "prenatal"}, "visit_date", True),
    ("/api/maternal/children/999999/visits", {"visit_type": "checkup"}, "visit_date", True),
    ("/api/maternal/children/999999/screenings", {"item": "hearing"}, "screen_date", True),
    ("/api/maternal/women-health", {"patient_id": 999999, "record_type": "premarital"}, "exam_date", True),
    # 监测领域填不存在的：合法日期越过这一道后在业务校验上 422（字符串 detail），不落库
    ("/api/publichealth/monitors", {"domain": "no-such-domain", "org_id": 999999, "indicator": "CO2",
                                    "value": 1, "threshold": 2}, "record_date", True),
    ("/api/quality/infection-reports",
     {"org_id": 999999, "patient_id": 999999, "infection_site": "no-such-site"}, "report_date", True),
    ("/api/tcm/preparation-batches", {"formula_id": 999999, "batch_no": "DATE-REG", "org_id": 999999,
                                      "quantity": 1, "produced_date": "2026-01-01"}, "expire_date", True),
    ("/api/contracts", {"patient_id": 999999, "org_id": 999999, "doctor_name": "李家医"}, "signed_date", True),
]

#: 三个都恰好 10 个字符——原先的长度卡全部放行。
BAD = ["2026/09/24", "2026-02-31", "abcdefghij"]


def _send(client, admin, path, body):
    method, _, url = path.rpartition(" ")
    return client.request(method or "POST", url, json=body, headers=admin)


def _case_id(case):
    """`maternal/children/visits.visit_date`：带上资源路径，同名字段不撞车。"""
    method, _, url = case[0].rpartition(" ")
    resource = "/".join(seg for seg in url.split("/")[2:] if not seg.isdigit())
    return f"{method + ':' if method else ''}{resource}.{case[2]}"


def _field_errors(resp, field):
    if resp.status_code != 422 or not isinstance(resp.json().get("detail"), list):
        return []
    return [e for e in resp.json()["detail"] if e.get("loc") == ["body", field]]


@pytest.mark.parametrize("path, body, field, optional", CASES, ids=[_case_id(c) for c in CASES])
def test_写错的日期在请求体校验层就被挡下(client, admin, path, body, field, optional):
    for bad in BAD:
        resp = _send(client, admin, path, {**body, field: bad})
        assert _field_errors(resp, field), (path, bad, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("path, body, field, optional", CASES, ids=[_case_id(c) for c in CASES])
def test_合法日期越过这一道校验(client, admin, path, body, field, optional):
    resp = _send(client, admin, path, {**body, field: "2026-09-24"})
    assert not _field_errors(resp, field), (path, resp.text[:200])
    if optional:
        blank = _send(client, admin, path, {**body, field: ""})
        assert not _field_errors(blank, field), ("可空字段留空不该被拒", path, blank.text[:200])
