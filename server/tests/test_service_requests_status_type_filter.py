"""统一申请单中心按状态 / 类型筛、标题照实写（P2-1311，第三十八批扫描 AB1-6）。

修前：后端 `workflows.unified_requests` 早就收 `status` / `request_type`（P2-162：落在最新 limit 条之外的按状态筛得到），
页面（`pages-mgmt.js::renderServiceRequests`）的筛选栏却只有患者号一格——卡片显示「待处理 1」，列出的最新 200 条里待处理
0 条，又无处按状态筛；标题「在办事项（251，列出最新 200 条）」把 250 条已完成也数了进去（total 是所有状态之和）。

修法：筛选栏加状态与类型两个下拉，选了带参数重取——状态的取值与文案用本页的 `UNIFIED_STATUS`（卡片、统一状态列同一张表；
回执的 by_status 只有计数没有文案），类型名取自回执的 `type_names`。标题照 P2-162 的口径照实写：不筛是「全部事项」，筛了是
「筛选结果」。另一个选项「默认只列待处理 + 处理中」改动更多：后端 status 只收一个值，要么页面取两次再合并计数与排序、
要么改后端接口。
"""
import os
import re

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _source() -> str:
    with open(os.path.join(STATIC, "pages-mgmt.js"), encoding="utf-8") as fh:
        return fh.read()


def _page() -> str:
    source = _source()
    start = source.index("async function renderServiceRequests()")
    return source[start:source.index("\n}\n", start)]


def test_筛选栏有状态与类型两个下拉():
    page = _page()
    form = re.search(r'<form class="inline" id="sr-form">(.*?)</form>', page, re.S).group(1)
    assert re.findall(r'<(?:input|select) name="(\w+)"', form) == ["patient_id", "status", "request_type"]   # 修前只有患者号
    # 取值不在前端另抄一份：状态用本页 UNIFIED_STATUS，类型名用回执的 type_names（见过的记在 SR_TYPE_NAMES）
    assert "options(Object.entries(UNIFIED_STATUS).map(([k, [text]]) => [k, text]), SR_FILTER.status)" in form
    assert "options(Object.entries(SR_TYPE_NAMES), SR_FILTER.request_type)" in form
    assert "Object.assign(SR_TYPE_NAMES, data.type_names);" in page


def test_提交时带上状态与类型重取():
    page = _page()
    assert 'SR_FILTER.status = f.get("status") || "";' in page
    assert 'SR_FILTER.request_type = f.get("request_type") || "";' in page
    assert "new URLSearchParams(Object.entries({ patient_id: pid, ...SR_FILTER }).filter(([, v]) => v))" in page
    assert "api(`/api/service-requests${query.toString() ? `?${query}` : \"\"}`)" in page
    # 筛选只留在内存里、不进存储（同 JOB_RUN_FILTER）
    assert 'const SR_FILTER = { status: "", request_type: "" };' in _source()
    assert "localStorage.setItem(\"medplat_sr_status" not in _source()


def test_标题照实写_不再叫在办事项():
    page = _page()
    assert "panel(`在办事项（" not in page   # 修前「在办事项（N）」的 N 含已完成、已取消
    assert 'panel(`${filtered ? "筛选结果" : "全部事项"}（${data.total}' in page
    assert "const filtered = Boolean(SR_FILTER.status || SR_FILTER.request_type);" in page


def test_状态下拉的取值就是后端的统一口径():
    """页面状态下拉取自 UNIFIED_STATUS：它的键与后端各类单据映射到的统一状态一一对应，后端加一档这里先红。"""
    from app.routers.workflows import STATUS_MAP

    table = re.search(r"const UNIFIED_STATUS = \{(.*?)\};", _source(), re.S).group(1)
    assert set(re.findall(r"(\w+): \[", table)) == {u for mapping in STATUS_MAP.values() for u in mapping.values()}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21311 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21311 患者", "id_card": "330106197001011311"}).json()["id"]
    return {"org": org, "patient": patient}


def test_最早那张待处理挤出最新200条_按状态与类型筛得到(client, admin, world):
    """页面筛选靠的就是这两个参数：默认清单只列最新 200 条，最早那张待处理的按状态 / 类型筛得到。"""
    from sqlalchemy import insert

    from app.models import ExamRequest, User

    first = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "imaging",
        "item_code": "P21311-CT", "item_name": "P21311 头颅CT"})
    assert first.status_code == 201, first.text
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.execute(insert(ExamRequest), [{
            "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "ecg", "item_code": "ECG",
            "item_name": "P21311 心电图", "status": "reported", "created_by": author} for _ in range(250)])
        db.commit()
    everything = client.get("/api/service-requests", headers=admin).json()
    assert (everything["total"], everything["returned"], everything["by_status"]) == (251, 200, {"pending": 1, "done": 250})
    assert not [i for i in everything["items"] if i["status"] == "pending"]   # 修前页面上看得到卡片、找不到这一行
    for params in ({"status": "pending"}, {"status": "pending", "request_type": "exam"}):
        got = client.get("/api/service-requests", headers=admin, params=params).json()
        assert [(i["request_type"], i["id"]) for i in got["items"]] == [("exam", first.json()["id"])], params
        assert got["total"] == 1
