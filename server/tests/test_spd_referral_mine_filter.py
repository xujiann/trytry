"""医生移动端「我发起的、被退回的」由接口按发起人筛；发起上转的患者下拉能检索、「重新发起」的患者一定选得上（P2-824，
第二十二批「页面查询参数 vs 后端」扫描 X4-4）。

移动端原先取机构最新 50 张退回单、在页面上按 `initiator_id === me.id` 挑——同一机构别人的退回单一多，本人被退回的单子和
退回意见就看不到（`routers/portal.py` 推送清单的说明里写明的反例：客户端拿到 N 条再自己筛）；`list_referrals` 没有按发起人
筛的参数，带 `initiator_id` 也照返全量。患者下拉取本人名下在管的前 100 份、不能检索，「重新发起」的预填要在这 100 份里
匹配，建档早的人选不到、预填落空。修后清单收 `mine=true`（与任务清单同一个意思），页面按它取；下拉加检索（接口早就收
keyword），重新发起的那位不在这一页里时照退回单补一项。
"""
from pathlib import Path

import pytest

from conftest import login

B = "/api/spd"
DOCTOR_JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs: dict[str, int] = {}
    for key, name, level, org_type, parent in (("county", "P2824 县医院", "county", "lead_hospital", None),
                                               ("town", "P2824 卫生院", "township", "township", "county"),
                                               ("village", "P2824 村卫生室", "village", "village", "town")):
        body = {"name": name, "level": level, "org_type": org_type}
        if parent:
            body["parent_id"] = orgs[parent]
        resp = client.post("/api/organizations", headers=admin, json=body)
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key, org in (("town", "town"), ("a", "village"), ("b", "village")):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2824_{key}", "password": "passw0rd1", "full_name": f"p2824_{key}", "role": "doctor",
            "org_id": orgs[org]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, f"p2824_{key}", "passw0rd1")
    cases = {"a": [], "b": []}
    for n, who in enumerate(("a", "b", "b", "b")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2824 患者{n}", "id_card": f"33012719580303282{n}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": orgs["village"]})
        assert enrolled.status_code == 201, enrolled.text
        case = client.post(f"{B}/referrals", headers=heads[who], json={
            "patient_id": patient, "program_code": "hypertension", "reason": f"P2824 血压控制不佳 {n}"})
        assert case.status_code == 201, case.text
        rejected = client.post(f"{B}/referrals/{case.json()['id']}/review", headers=heads["town"],
                               json={"action": "reject", "opinion": "请补充近一周血压记录"})
        assert rejected.status_code == 200, rejected.text
        cases[who].append(case.json()["id"])
    return {"heads": heads, "cases": cases}


def test_mine只列本人发起的(client, world):
    heads, cases = world["heads"], world["cases"]

    def ids(headers, **params):
        rows = client.get(f"{B}/referrals", headers=headers, params={"status": "rejected", "limit": 50, **params})
        assert rows.status_code == 200, rows.text
        return {r["id"] for r in rows.json()}

    everyone = ids(heads["a"])
    assert set(cases["a"]) | set(cases["b"]) <= everyone   # 不带 mine：同机构的都在
    assert ids(heads["a"], mine="true") == set(cases["a"])   # 修前照返全量（参数不认）
    assert ids(heads["b"], mine="true") == set(cases["b"])
    assert ids(heads["a"], mine="true", limit=1) == set(cases["a"])   # 别人的退回单再多也挤不掉本人的


def test_页面按mine取_下拉能检索_重新发起补一项():
    start = DOCTOR_JS.index("async function loadSpdReferral(")
    page = DOCTOR_JS[start:DOCTOR_JS.index("\n}\n", start)]
    assert 'api("/api/spd/referrals?status=rejected&mine=true&limit=10")' in page
    assert "initiator_id === me.id" not in page   # 修前在页面上挑
    start = DOCTOR_JS.index("function spdReferralPatientOptions(")
    options = DOCTOR_JS[start:DOCTOR_JS.index("\n}\n", start)]
    assert "if (prefill.patient_id && !opts.some((o) => o.value === pick))" in options
    start = DOCTOR_JS.index("async function openSpdReferralForm(")
    form = DOCTOR_JS[start:DOCTOR_JS.index("\n}\n", start)]
    assert "data-spd-ref-find" in form and '<input name="kw"' in form   # 修前没有检索
    assert "`/api/spd/enrollments?limit=100&${mineQuery}${kw ? `&keyword=${encodeURIComponent(kw)}` : \"\"}`" in form
