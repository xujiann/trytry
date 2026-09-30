"""传染病病种编码按比对键对目录、按比对键分组：写法不同的同一个编码（夹零宽字符、小写、带尾随空格）照样回填分类、进迟报
清单、凑进多点预警（P2-1146，第三十三批扫描 A3-2）。

「病种编码」是手输框，提交前不 trim；回填分类、报告卡 / 法定导出、迟报清单原先都按原样对目录，多点预警按原样分组。
2026-09-30 开发库实测：霍乱发病 3 天后才报，`A00` 回填甲类、进迟报清单；`A00`+U+200B、`a00`、`A00 ` 三种写法 category
都是空串，迟报清单只有 `A00` 那张，报告卡印 `{'category_name': '目录外', 'report_hours': None, 'late': None}`；流感 3 例
`J11` + 2 例 `J11`+U+200B 分属两家机构，多点预警 []。

修法：目录元信息按 `texttypes.code_key` 建键取值（回填分类、报告卡、导出、迟报清单共用 `_disease_meta`），多点预警把窗口内
的行取出来按 `code_key` 分组；落库的编码照原样，存量不改。
"""
import csv
import io

import pytest

B = "/api/infectious"
ZWSP = "\u200b"
VARIANTS = ["A00" + ZWSP, "a00", "A00 "]
VARIANT_IDS = ["夹零宽字符", "小写", "尾随空格"]


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [client.post("/api/organizations", headers=admin, json={
        "name": f"P21146 卫生院{i}", "org_type": "township", "level": "township"}).json()["id"] for i in (1, 2)]


def _report(client, admin, org, code, name, onset):
    resp = client.post(f"{B}/cases", headers=admin, json={
        "org_id": org, "disease_code": code, "disease_name": name, "onset_date": onset})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("code", VARIANTS, ids=VARIANT_IDS)
def test_霍乱编码写法不同_照样回填甲类_进迟报清单(client, admin, orgs, code):
    case = _report(client, admin, orgs[0], code, "霍乱", "2026-09-01")   # 发病后很多天才报：甲类（2 小时）迟报
    assert case["category"] == "A", case   # 修前 ''
    assert case["disease_code"] == code   # 落库的编码照原样
    late = {row["case_id"]: row for row in client.get(f"{B}/late-reports", headers=admin).json()}
    assert case["id"] in late, late   # 修前不在清单里
    assert (late[case["id"]]["category"], late[case["id"]]["report_hours"]) == ("A", 2)
    card = client.get(f"{B}/cases/{case['id']}/report-card", headers=admin).json()
    assert (card["category_name"], card["report_hours"], card["late"]) == ("甲类", 2, True), card   # 修前 目录外 / None / None
    assert card["disease_name"] == "霍乱"
    exported = client.get(f"{B}/cases/export.csv", headers=admin, params={"late_only": "true"})
    assert exported.status_code == 200, exported.text
    rows = {int(r["卡片编号"]): r for r in csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff")))}
    assert rows[case["id"]]["及时性"] == "迟报", rows.get(case["id"])   # 修前「只导迟报」漏掉这张卡


@pytest.mark.parametrize("variant, month", [("J11" + ZWSP, "01"), ("j11", "02"), ("J11 ", "03")], ids=VARIANT_IDS)
def test_多点预警_写法不同的同一个编码并成一组(client, admin, orgs, variant, month):
    onset = f"2026-{month}-14"
    for _ in range(3):
        _report(client, admin, orgs[0], "J11", "流行性感冒", onset)
    for _ in range(2):
        _report(client, admin, orgs[1], variant, "流感", onset)
    resp = client.get(f"{B}/alerts", headers=admin,
                      params={"window_days": 7, "threshold": 5, "today": f"2026-{month}-15"})
    assert resp.status_code == 200, resp.text
    # 修前 []：3 例与 2 例按原样分成两组，都不到阈值 5
    assert [(a["disease_code"], a["disease_name"], a["case_count"], a["org_count"], a["severity"])
            for a in resp.json()] == [("J11", "流行性感冒", 5, 2, "high")]


def test_目录外病种照旧不回填_预警照旧取报告里的写法(client, admin, orgs):
    case = _report(client, admin, orgs[0], "P21146X", "P21146 目录外新发病", "2026-04-14")
    assert case["category"] == ""
    card = client.get(f"{B}/cases/{case['id']}/report-card", headers=admin).json()
    assert (card["category_name"], card["report_hours"], card["late"]) == ("目录外", None, None)
    _report(client, admin, orgs[1], "p21146x", "P21146 目录外新发病", "2026-04-14")
    alerts = client.get(f"{B}/alerts", headers=admin, params={"window_days": 7, "threshold": 2, "today": "2026-04-15"})
    assert [(a["disease_code"], a["case_count"], a["org_count"]) for a in alerts.json()] == [("P21146X", 2, 2)]
