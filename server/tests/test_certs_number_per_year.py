"""证明编号的顺序号按「前缀＋年份」各自数，跨年第一张从 000001 起（P2-897，第二十四批「时间窗口的边界」扫描 Z1-12）。

注释写的是「类型前缀 + 年份 + 6 位顺序号」，顺序号却是该类型全部证明数 + 1：12-30、12-31 签的是 B2026000001、
B2026000002，元旦那张成了 B2027000003。同型的医废追溯码按前缀各自计数。修后取同年已用的最大号加一；存量编号不动
（同年里已有的号续在最大号之后）。
"""
from datetime import date

from conftest import freeze_business_date


def test_跨年第一张从1起_同年续在最大号之后(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2897 妇幼保健院", "org_type": "township", "level": "township"}).json()["id"]

    def issue(day):
        with freeze_business_date(day):
            got = client.post("/api/certs", headers=admin, json={
                "cert_type": "birth", "name": "P2897 新生儿", "event_date": day.isoformat(), "org_id": org})
        assert got.status_code == 201, got.text
        return got.json()["cert_no"]

    numbers = [issue(day) for day in (date(2031, 12, 30), date(2031, 12, 31), date(2032, 1, 1), date(2032, 1, 2))]
    assert numbers[2:] == ["B2032000001", "B2032000002"]   # 修前 B2032000003 / B2032000004
    assert numbers[:2] == [f"B2031{n:06d}" for n in (int(numbers[0][5:]), int(numbers[0][5:]) + 1)]
