"""迟报清单页把发病到报告的天数写成「迟报 N 天」（P2-1629，第四十八批扫描 AL3-7，P2-470 的同族余项）。

`days_late` 是发病到报告隔了几天，不是超出法定时限几天（P2-470 已写明）：限 24 小时报告的肺结核前天发病、今天报告，
`days_late=2`，按系统自己的按天折算只超了 1 天，`renderInfectiousDir` 的迟报清单却印「迟报 2 天」——乙丙类每一行都
多说 1 天。报告卡弹层（`pages-clinical.js`）早按 P2-470 改成「发病后 N 天报告」，这一处漏了。修后两页同一句话；
后端 CSV 表头「迟报天数」当时有意冻结字节，不动。
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _function_body(source: str, name: str) -> str:
    start = source.index(f"async function {name}(")
    nxt = re.search(r"\n(?:async )?function |\nconst ", source[start + 1:])
    return source[start: start + 1 + nxt.start()] if nxt else source[start:]


def test_迟报清单不再把发病到报告的天数写成迟报几天():
    body = _function_body(_read("pages-public.js"), "renderInfectiousDir")
    assert "迟报 ${l.days_late} 天" not in body   # 修前
    assert "（发病后 ${l.days_late} 天报告）" in body


def test_与报告卡弹层同一句话():
    clinical = _read("pages-clinical.js")
    assert "（发病后 ${c.days_late} 天报告）" in clinical   # P2-470 报告卡那一句，两页说法要一致
    public = _function_body(_read("pages-public.js"), "renderInfectiousDir")
    assert "（发病后 ${l.days_late} 天报告）" in public


def test_接口端_限24小时的病种前天发病今天报告_进迟报清单且隔2天(client, admin):
    from datetime import timedelta

    from app import clock

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21629 迟报医院", "org_type": "township", "level": "township"}).json()["id"]
    diseases = client.get("/api/infectious/diseases", headers=admin).json()
    within_day = next(d for d in diseases if d["report_hours"] == 24)
    two_days_ago = (clock.today() - timedelta(days=2)).isoformat()
    case = client.post("/api/infectious/cases", headers=admin, json={
        "org_id": org, "disease_code": within_day["code"], "disease_name": within_day["name"],
        "onset_date": two_days_ago})
    assert case.status_code == 201, case.text
    rows = client.get("/api/infectious/late-reports", headers=admin).json()
    row = next(r for r in rows if r["case_id"] == case.json()["id"])
    # 限 24 小时、隔 2 天：超时限的只有 1 天，页面原先却印「迟报 2 天」
    assert (row["report_hours"], row["days_late"]) == (24, 2)
