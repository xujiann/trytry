"""什么结果都没录的体检照样标「正常」、照样能总检，打出一份带验真码、分项栏写「存量记录仅有汇总小结」的报告（P2-1403，
第四十一批扫描 AE3-2）。

页面登记只填患者、机构、日期（体检结论、异常项都是选填）就返回 201。修前：清单这一行是绿色「正常」；总检「未见明显异常」
返回 200；打印件分项栏印「无分项结果（存量记录仅有汇总小结）」——刚登记的新记录被写成存量，汇总小结一栏却是「—」。
模块 docstring 写的是「分项 / 汇总录完后由总检医师出结论」。

修后：没有分项、汇总小结与异常项都是空的（只填空格的不算录了）体检，总检 409「尚无体检结果，不能出总检结论」，有其一的照常
总检；清单行多回一个 `has_results`，页面没异常的行据此画「正常」或「未录结果」；打印件不再印「存量」（按行认不出哪条是 B2
之前的存量，见 `print_checkup_report` 的注释）：写了汇总小结的印「无分项结果（仅有汇总小结）」，没写的印「未录分项结果」。
"""
from pathlib import Path

import pytest

from conftest import login

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")

ITEM = {"item_code": "GLU", "item_name": "空腹血糖", "result_value": "5.2", "unit": "mmol/L", "ref_range": "3.9-6.1",
        "abnormal": False}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21403 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21403 受检者", "id_card": "330127195001011403", "gender": "男"}).json()["id"]
    for username, role in (("p21403_ph", "public_health"), ("p21403_doc", "doctor")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "role": role, "org_id": org})
        assert made.status_code in (200, 201), made.text
    ph = login(client, "p21403_ph", "pass123456")

    def checkup(**fields):
        resp = client.post("/api/checkups", headers=ph, json={
            "patient_id": patient, "org_id": org, "exam_date": "2026-10-01", **fields})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {
        "patient": patient,
        "ph": ph,
        "doc": login(client, "p21403_doc", "pass123456"),
        "ids": {
            "empty": checkup(),                                  # 页面形态：只填患者、机构、日期
            "blank": checkup(summary="   ", abnormal_items=" "),  # 只填了空格
            "summary": checkup(summary="各项正常"),
            "abnormal": checkup(abnormal_items="血压 150/95"),
            "items": checkup(items=[ITEM]),                       # 分项都正常、没写汇总
        },
    }


def _review(client, world, key):
    return client.post(f"/api/checkups/{world['ids'][key]}/review", headers=world["doc"],
                       json={"final_conclusion": "未见明显异常，建议一年后复查"})


def _rows(client, world):
    rows = client.get("/api/checkups", headers=world["doc"], params={"patient_id": world["patient"]}).json()
    return {r["id"]: r for r in rows}


@pytest.mark.parametrize("key", ["empty", "blank"])
def test_什么结果都没录的体检总检409_结论不落库(client, world, key):
    resp = _review(client, world, key)
    assert resp.status_code == 409, resp.text                     # 修前 200，打出一份没有任何测值的结论报告
    assert resp.json()["detail"] == "尚无体检结果，不能出总检结论"
    assert _rows(client, world)[world["ids"][key]]["reviewed"] is False


@pytest.mark.parametrize("key", ["summary", "abnormal", "items"])
def test_有分项_汇总小结_异常项其一的照常总检(client, world, key):
    resp = _review(client, world, key)
    assert resp.status_code == 200, resp.text
    assert resp.json()["final_conclusion"] == "未见明显异常，建议一年后复查"


def test_清单行带has_results_页面没异常的行据此画正常或未录结果(client, world):
    rows = _rows(client, world)
    got = {key: rows[cid]["has_results"] for key, cid in world["ids"].items()}   # 修前清单行没有这个键
    assert got == {"empty": False, "blank": False, "summary": True, "abnormal": True, "items": True}
    assert 'const CHK_RESULT = { ok: ["正常", "green"], none: ["未录结果", ""] };' in PAGE
    render = PAGE[PAGE.index("async function renderCerts()"):]
    render = render[:render.index("\nasync function ")]
    assert 'statusTag(CHK_RESULT, c.has_results ? "ok" : "none")' in render
    assert """'<span class="tag green">正常</span>'""" not in render   # 修前没异常一律画绿色「正常」
    # 未录结果的行不摆「总检」：后端对它 409，摆出来点了也只是在框里报错
    assert 'canReview && c.has_results ? `<button class="btn secondary" data-chkreview=' in render


def _items_cell(client, world, key):
    html = client.get(f"/api/print/checkups/{world['ids'][key]}", headers=world["doc"]).text
    section = html[html.index("<h3>分项结果</h3>"):]
    return html, section[:section.index("</tbody>")]


def test_打印件_没写汇总小结的印未录分项结果_存量二字不再印(client, world):
    for key in ("empty", "blank"):
        html, cell = _items_cell(client, world, key)
        assert "未录分项结果" in cell, cell
        assert "存量" not in html and "仅有汇总小结" not in html   # 修前「无分项结果（存量记录仅有汇总小结）」
    html, cell = _items_cell(client, world, "abnormal")             # 只填了异常项：没有汇总小结可言，异常项照旧印在下面
    assert "未录分项结果" in cell and "存量" not in html
    tip = html[html.index("<h3>异常项提示</h3>"):]
    assert "血压 150/95" in tip[:tip.index("</div></div>")]


def test_打印件_写了汇总小结的印仅有汇总小结_有分项的照旧列分项(client, world):
    html, cell = _items_cell(client, world, "summary")
    assert "无分项结果（仅有汇总小结）" in cell and "存量" not in html
    html, cell = _items_cell(client, world, "items")
    assert "空腹血糖" in cell and "无分项结果" not in cell and "未录分项结果" not in cell
