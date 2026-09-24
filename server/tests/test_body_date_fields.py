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
CASES = [
    ("/api/analytics/outbound-visits",
     {"patient_id": 999999, "external_org_name": "市一院"}, "visit_date", False),
]

#: 三个都恰好 10 个字符——原先的长度卡全部放行。
BAD = ["2026/09/24", "2026-02-31", "abcdefghij"]


def _field_errors(resp, field):
    if resp.status_code != 422 or not isinstance(resp.json().get("detail"), list):
        return []
    return [e for e in resp.json()["detail"] if e.get("loc") == ["body", field]]


@pytest.mark.parametrize("path, body, field, optional", CASES, ids=[c[2] for c in CASES])
def test_写错的日期在请求体校验层就被挡下(client, admin, path, body, field, optional):
    for bad in BAD:
        resp = client.post(path, json={**body, field: bad}, headers=admin)
        assert _field_errors(resp, field), (path, bad, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("path, body, field, optional", CASES, ids=[c[2] for c in CASES])
def test_合法日期越过这一道校验(client, admin, path, body, field, optional):
    resp = client.post(path, json={**body, field: "2026-09-24"}, headers=admin)
    assert not _field_errors(resp, field), (path, resp.text[:200])
    if optional:
        blank = client.post(path, json={**body, field: ""}, headers=admin)
        assert not _field_errors(blank, field), ("可空字段留空不该被拒", path, blank.text[:200])
