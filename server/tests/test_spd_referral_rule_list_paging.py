"""慢专病转诊规则清单截在 200 条、不带总数、不收 offset；试算却过全部启用规则（P2-1610，第四十七批扫描 AK4-6）。

`list_referral_rules` 原先 `.limit(200).all()`：建到第 201 条规则，清单只回前 200 条、没有 X-Total-Count，`offset` 参数
不存在、原样忽略——第 201 条在「逐级转诊闭环」页的规则表里看不到、点不了「编辑」停不了；`check_referral_rules` 却
`query.all()` 过全部启用规则，第 201 条照样命中、勾了「命中即开上转单」照样开单。

修法：改走 `deps.paginate`（与同文件转诊清单、超时预警同一签名：`response, offset, limit`），缺省一页仍是 200 条、按规则
编号升序，前 200 条与原先逐字节相同；页面规则表续页取全（shared.js 的 `fetchAllPages`，同 P2-1546）。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from jssrc import strip_comments

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def rules(client, admin):
    """201 条规则：前 200 条「年龄 ≥ 200」谁都不命中，第 201 条（R200）「年龄 ≥ 18」人人命中。"""
    from app.spd.models import SpdReferralRule

    with SessionLocal() as db:
        rows = [SpdReferralRule(code=f"P21610_R{i:03d}", name=f"P21610 规则{i}",
                                program_code="hypertension" if i % 2 == 0 else "",
                                conditions=[{"field": "age", "op": ">=", "value": 200 if i < 200 else 18, "label": ""}])
                for i in range(201)]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]


def test_第201条在第二页_总数头201(client, admin, rules):
    first = client.get(f"{B}/referral-rules", headers=admin)
    assert first.status_code == 200, first.text
    assert first.headers.get("X-Total-Count") == "201"   # 修前没有这个头
    assert [r["id"] for r in first.json()] == rules[:200]   # 缺省一页仍是 200 条、按编号升序
    second = client.get(f"{B}/referral-rules", headers=admin, params={"offset": 200})
    assert second.status_code == 200, second.text
    assert [r["code"] for r in second.json()] == ["P21610_R200"]   # 修前 offset 被忽略、又是前 200 条
    assert second.headers.get("X-Total-Count") == "201"


def test_前200条与原先逐字节相同(client, admin, rules):
    from app.spd.models import SpdReferralRule
    from app.spd.routers.referral import _rule_out

    with SessionLocal() as db:
        expected = [_rule_out(r) for r in db.query(SpdReferralRule).order_by(SpdReferralRule.id).limit(200).all()]
    assert client.get(f"{B}/referral-rules", headers=admin).json() == expected


def test_筛选之后的总数按筛选算(client, admin, rules):
    got = client.get(f"{B}/referral-rules", headers=admin,
                     params={"program_code": "hypertension", "active": "true", "limit": 10, "offset": 95})
    assert got.status_code == 200, got.text
    assert got.headers.get("X-Total-Count") == "101"   # 偶数编号 0..200 共 101 条
    assert [r["code"] for r in got.json()] == ["P21610_R190", "P21610_R192", "P21610_R194", "P21610_R196",
                                                "P21610_R198", "P21610_R200"]


def test_试算命中的第201条清单里翻得到(client, admin, rules):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21610 患者", "id_card": "330127196001011610", "gender": "男", "birth_date": "1960-01-01"}).json()
    hits = client.post(f"{B}/referral-rules/check", headers=admin, json={"patient_id": patient["id"]}).json()["hits"]
    assert [h["rule"]["code"] for h in hits] == ["P21610_R200"]
    listed = client.get(f"{B}/referral-rules", headers=admin, params={"offset": 200}).json()
    assert [r["id"] for r in listed] == [hits[0]["rule"]["id"]]


def test_页面规则表续页取全():
    pages = strip_comments((STATIC / "pages-spd.js").read_text(encoding="utf-8"))
    start = pages.index("async function renderSpdReferral(")
    body = pages[start:pages.index("\nasync function ", start + 1)]
    assert 'fetchAllPages(api, "/api/spd/referral-rules")' in body   # 修前 api("/api/spd/referral-rules")，只有第一页
    assert 'api("/api/spd/referral-rules")' not in body
