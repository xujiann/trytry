"""多点预警的病原表只按舍入后的阳性率排、没有次键：并列时的先后全凭取数顺序（P2-1466，P2-1437 症候群全序的同类跟进）。

`multi_point_alerts` 的症候群预警在 P2-1437 换成了全序（分数比 → 例数 → 机构 → 行 id），病原预警还是
`key=-(positive_rate_pct or 0)`：`positive_rate_pct` 是四舍五入到两位的百分比，真实比值不同而舍入后相同（10/30 = 33.333…%
与 3333/10000 = 33.33%）就成了并列；比值本身相同（2/10 与 4/20）也是并列。并列的先后全凭取数顺序，而取数没有 ORDER BY——
SQLite 上是插入顺序，PG 上同一请求两次可以不同，表头那一行跟着跳。

修后与症候群同一套全序：分数比（不经舍入）从高到低，比值相同再按阳性数降序、机构 id，最后按行 id 倒序。这里每组都**先插**
修前会排在前面的那一行（SQLite 按插入顺序回），修前红、修后绿。
"""
import pytest

B = "/api/surveillance"


def _org(client, admin, name):
    return client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"]


def _pathogen(client, admin, org, positive, tested, day, name="甲型流感"):
    resp = client.post(f"{B}/pathogens", headers=admin, json={
        "org_id": org, "pathogen_name": name, "tested_count": tested, "positive_count": positive, "record_date": day})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _order(client, admin, day, ids):
    """这一天的病原预警里，本用例那几行的先后（别的用例同日的行不算）。"""
    alerts = client.get(f"{B}/alerts", headers=admin, params={"today": day, "days": 1}).json()["pathogen_alerts"]
    return [a["id"] for a in alerts if a["id"] in ids]


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [_org(client, admin, f"P21466 卫生院{n}") for n in "甲乙丙"]


def test_舍入后相同的阳性率按真实比值排(client, admin, orgs):
    day = "2026-08-11"
    lower = _pathogen(client, admin, orgs[0], 3333, 10000, day)   # 33.33% 整
    higher = _pathogen(client, admin, orgs[0], 10, 30, day)       # 33.333…%，舍入后也是 33.33%
    rates = {a["id"]: a["positive_rate_pct"] for a in
             client.get(f"{B}/alerts", headers=admin, params={"today": day, "days": 1}).json()["pathogen_alerts"]}
    assert rates[lower] == rates[higher] == 33.33   # 出参的百分比照旧舍入，只是不再拿它排序
    assert _order(client, admin, day, {lower, higher}) == [higher, lower]   # 修前按插入顺序 [lower, higher]


def test_比值相同按阳性数降序_再按机构id_同一机构再按行id倒序(client, admin, orgs):
    day = "2026-08-12"
    few = _pathogen(client, admin, orgs[0], 2, 10, day)                   # 20%，阳性 2 例
    many = _pathogen(client, admin, orgs[0], 4, 20, day, "乙型流感")       # 同为 20%，阳性 4 例：排在前面
    later_org = _pathogen(client, admin, orgs[2], 4, 20, day)             # 比值、阳性数都与 many 相同：机构 id 大的在后
    older = _pathogen(client, admin, orgs[1], 4, 20, day)
    newer = _pathogen(client, admin, orgs[1], 4, 20, day, "副流感")        # 同机构同比值同阳性数：后报的（行 id 大）在前
    ids = {few, many, later_org, older, newer}
    assert _order(client, admin, day, ids) == [many, newer, older, later_org, few]
    assert _order(client, admin, day, ids) == _order(client, admin, day, ids)   # 再取一次，先后不变


def test_送检不满10份或阳性率不到10_不进预警_排序不碰分母(client, admin, orgs):
    day = "2026-08-13"
    small = _pathogen(client, admin, orgs[0], 5, 9, day)        # 送检 9 份：小样本，不列
    low = _pathogen(client, admin, orgs[0], 0, 50, day)         # 阳性率 0：不列
    hit = _pathogen(client, admin, orgs[0], 5, 10, day)
    assert _order(client, admin, day, {small, low, hit}) == [hit]
