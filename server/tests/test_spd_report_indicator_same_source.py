"""报告的「考核指标」段落与正式考核分同源（P2-529，第十批「同源数字承诺」扫描 X2-1）。

`reporting._indicator` 的 docstring、报告模板页与两本培训手册都写着「报表数字与考核数字同源」，段落却自己挑指标版本：
取编号最大的那一版、不看生效日期——2031 年才生效的新口径（连同它的名称与公式）印进本月报告，与本期正式考核分对不上；
全都还没生效时照样出数（正式计分写「指标在本期尚未生效」、分数为 0）。按村医 / 团队 / 医师计分的指标也按机构汇总出
一个数，那是任何一份考核分里都没有的数（一家村卫生室两位村医各 100、0，报告印 50）。

修后版本选择与计分共用 `assess.effective_versions`；本期没生效的写明「尚未生效」、不出数；不按机构计分的指标写明
不能引用、不出数。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2529 卫生院", "org_type": "township", "level": "township"}).json()["id"]

    def indicator(code, tag, formula, effective_from="", object_type="org"):
        resp = client.post(f"{B}/indicators", headers=admin, json={
            "code": code, "name": f"P2529 {code} {tag}", "data_source": "task", "object_type": object_type,
            "formula": formula, "version": tag, "effective_from": effective_from})
        assert resp.status_code == 201, resp.text

    indicator("p2529_rate", "v1", "10")
    indicator("p2529_rate", "v2", "20", "2026-06-01")
    indicator("p2529_rate", "v3", "30", "2031-01-01")
    indicator("p2529_future", "v1", "40", "2031-01-01")
    indicator("p2529_vd", "v1", "50", object_type="village_doctor")
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "p2529_plan", "name": "P2529 方案", "level": "township", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": "p2529_rate", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    return {"org": org, "plan": plan.json()["id"]}


def _section(client, admin, world, code, period):
    tpl_code = f"p2529_{code}_{period}".replace("-", "")[:32]
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": tpl_code, "name": f"P2529 {code} {period}", "period": "monthly",
        "sections": [{"key": "indicator", "indicator_code": code, "title": "考核指标", "period": period}]})
    assert tpl.status_code == 201, tpl.text
    inst = client.post(f"{B}/report-instances", headers=admin, json={"template_code": tpl_code, "org_id": world["org"]})
    assert inst.status_code == 201, inst.text
    return inst.json()["content"]["sections"][0]


def _official(client, admin, world, period):
    run = client.post(f"{B}/scores/run", headers=admin, json={
        "plan_id": world["plan"], "period": period, "object_ids": [world["org"]]})
    assert run.status_code == 200, run.text
    rows = client.get(f"{B}/scores", headers=admin, params={"plan_id": world["plan"], "period": period}).json()
    return client.get(f"{B}/scores/{rows[0]['id']}", headers=admin).json()["detail"][0]


def test_报告与正式考核分取同一版本(client, admin, world):
    for period, version in (("2026-09", "v2"), ("2026-03", "v1")):
        section = _section(client, admin, world, "p2529_rate", period)
        official = _official(client, admin, world, period)
        # 修前：报告取编号最大的 v3（2031 年才生效），印 30
        assert (section["value"], section["version"]) == (official["value"], official["version"]) \
            == ({"v2": 20.0, "v1": 10.0}[version], version), (period, section)


def test_本期还没生效的_写明尚未生效不出数(client, admin, world):
    section = _section(client, admin, world, "p2529_future", "2026-09")
    assert "value" not in section, section   # 修前照样出数 40
    assert section["note"] == "指标 p2529_future 在本期（2026-09）尚未生效"


def test_不按机构计分的指标_不按机构汇总出数(client, admin, world):
    section = _section(client, admin, world, "p2529_vd", "2026-09")
    assert "value" not in section, section   # 修前按机构汇总印出一个考核分里没有的数
    assert "按村医计分" in section["note"]
