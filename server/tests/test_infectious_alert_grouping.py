"""传染病多点预警按病种编码认「同病种」，窗口「7 天」就是 7 个日历日（P2-159）。

预警的说明写「滑动窗口内同病种病例数≥阈值，且涉及机构数≥2 时升级预警」，用户手册写「7 天窗口内同病种≥5 例且涉及
2 家以上机构升级为高风险」。实现按（编码, 名称）分组——名称是报告时手填的自由文本，同是 J11，甲院写「流行性感冒」、
乙院写「流感」，两家各 3 例被拆成两组、都不到阈值，跨机构的聚集一条预警都不出；窗口从今天往前减 7 天、两头都含，
实际是 8 天（症候群那边的多点预警早就是 days − 1）。
"""
import pytest

TODAY = "2026-09-26"


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [client.post("/api/organizations", headers=admin, json={
        "name": f"P2159 卫生院{i}", "org_type": "township", "level": "township"}).json()["id"] for i in (1, 2)]


def _report(client, admin, org, code, name, onset):
    resp = client.post("/api/infectious/cases", headers=admin, json={
        "org_id": org, "disease_code": code, "disease_name": name, "onset_date": onset})
    assert resp.status_code == 201, resp.text


def _alerts(client, admin, threshold):
    resp = client.get(f"/api/infectious/alerts?window_days=7&threshold={threshold}&today={TODAY}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_同码不同名的并成一组_跨机构升级(client, admin, orgs):
    for _ in range(3):
        _report(client, admin, orgs[0], "P2159A", "流行性感冒", "2026-09-24")
        _report(client, admin, orgs[1], "P2159A", "流感", "2026-09-25")
    rows = [r for r in _alerts(client, admin, 5) if r["disease_code"] == "P2159A"]
    # 修前 []：两个写法各 3 例，都不到阈值
    assert [(r["case_count"], r["org_count"], r["severity"]) for r in rows] == [(6, 2, "high")]


def test_七天窗口是七个日历日(client, admin, orgs):
    _report(client, admin, orgs[0], "P2159B", "手足口病", "2026-09-19")   # 今天往前第 7 天：窗口外
    _report(client, admin, orgs[0], "P2159B", "手足口病", "2026-09-20")   # 第 6 天：窗口内（含今天共 7 天）
    rows = [r for r in _alerts(client, admin, 1) if r["disease_code"] == "P2159B"]
    assert [r["case_count"] for r in rows] == [1]   # 修前 2：09-19 也算进了「7 日」
