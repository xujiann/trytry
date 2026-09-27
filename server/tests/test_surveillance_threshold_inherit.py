"""症候群日报不给阈值就把阈值清零：同日补报例数更多，预警反而消失；页面预填 0，每次上报都关掉预警（P1-182）。

模块口径 1 写着「阈值存在记录上，不是全局常量」——阈值是这家机构对这个症候群的设定。可 `SyndromeIn.threshold` 默认 0，
同日重复上报按覆盖（口径 2），覆盖时连阈值一起写：早上报发热 8 例、阈值 5，超阈值、进多点触发预警；中午补报成 12 例、
没再填阈值，201 回 `threshold: 0, alert: false`，预警清单变空——例数更多，预警没了。页面上的阈值框还预填了 0，
不重新敲一遍阈值，每一次从页面上报都等于关掉这家机构的预警。

修法：不给阈值即沿用——同日补报沿用当天那条的阈值，新的一天沿用这家机构这个症候群最近一次上报的阈值；明确给 0
仍是「不设阈值」。页面阈值框不再预填 0（留空沿用）。
"""
import pathlib

import pytest

B = "/api/surveillance"
STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P1182 发热门诊卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _report(client, admin, org, syndrome="fever", **body):
    resp = client.post(f"{B}/syndromes", headers=admin, json={"org_id": org, "syndrome": syndrome, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _alerting(client, admin, day):
    alerts = client.get(f"{B}/alerts", headers=admin, params={"today": day, "days": 1}).json()
    return [(a["case_count"], a["threshold"]) for a in alerts["syndrome_alerts"] if a["record_date"] == day]


def test_同日补报不填阈值_沿用当天的阈值_预警照出(client, admin, org):
    morning = _report(client, admin, org, case_count=8, threshold=5, record_date="2026-09-20")
    assert (morning["threshold"], morning["alert"]) == (5, True)
    corrected = _report(client, admin, org, case_count=12, record_date="2026-09-20")
    assert corrected["overwritten"] is True
    assert (corrected["case_count"], corrected["threshold"], corrected["alert"]) == (12, 5, True)   # 修前 (12, 0, False)
    assert _alerting(client, admin, "2026-09-20") == [(12, 5)]                                   # 修前 []


def test_新的一天不填阈值_沿用最近一次的阈值(client, admin, org):
    def report(day, **extra):
        return _report(client, admin, org, syndrome="respiratory", case_count=5, record_date=day, **extra)

    report("2026-09-10", threshold=4)
    report("2026-09-12", threshold=9)
    assert report("2026-09-13")["threshold"] == 9   # 修前 0：新的一天不填阈值，这家机构的预警就关了
    # 补报夹在中间的一天：取那天及以前最近的一次（09-10 的 4），不拿之后才改的 9
    assert report("2026-09-11")["threshold"] == 4
    # 补报最早的一天：那天及以前一条都没有，取眼下最新的设定
    assert report("2026-09-05")["threshold"] == 9


def test_明确给0仍是不设阈值_从没设过的沿用成0(client, admin, org):
    off = _report(client, admin, org, case_count=30, threshold=0, record_date="2026-09-24")
    assert (off["threshold"], off["alert"]) == (0, False)
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P1182 从未设阈值的村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    first = _report(client, admin, other, case_count=2, record_date="2026-09-24")
    assert (first["threshold"], first["alert"]) == (0, False)


def test_页面阈值框不再预填0():
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    form = source[source.index('id="syn-form"'): source.index("</form>", source.index('id="syn-form"'))]
    threshold_input = next(line for line in form.splitlines() if 'name="threshold"' in line)
    assert 'value="0"' not in threshold_input, threshold_input   # 修前预填 0：不重敲阈值，每次上报都关掉预警
