"""建报告模板的段落可选项取自段落注册表（P2-93 接建模板入口时补）。

报告模板的段落是 `{"key": 段落码, "title": ...}`，段落码要在 `spd/reporting.py` 的注册表里（不在的段落照样存得进、
生成时只出一句「未配置取数口径」）。管理端要让人勾段落，段落表若在前端另抄一份，县里注册了本地段落页面上就勾
不到——所以注册时带上名称，经 `GET /api/spd/meta` 的 `report_sections` 给界面。
"""
import pytest

from app.spd.reporting import registered_sections

B = "/api/spd"


def test_元数据给出全部已注册的段落_都有中文名(client, admin):
    body = client.get(f"{B}/meta", headers=admin).json()
    got = {x["key"]: x["name"] for x in body["report_sections"]}
    assert set(got) == set(registered_sections())
    assert all(name and name != key for key, name in got.items()), got   # 页面上不显示英文段落码


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P293 报告院", "org_type": "township", "level": "township"}).json()["id"]


def test_按界面的写法建模板_生成的报告逐段有内容(client, admin, org):
    """界面按勾选顺序给段落、标题取段落名；考核指标段落带指标编码、每个一段。"""
    created = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P293_RPT", "name": "P293 运行月报", "period": "monthly", "scope_level": "grassroots",
        "sections": [{"key": "summary", "title": "总体概览"}, {"key": "referral", "title": "转诊闭环"},
                     {"key": "indicator", "indicator_code": "followup_rate", "title": "考核指标 followup_rate"}]})
    assert created.status_code == 201, created.text
    report = client.post(f"{B}/report-instances", headers=admin,
                         json={"template_code": "P293_RPT", "org_id": org})
    assert report.status_code == 201, report.text
    sections = report.json()["content"]["sections"]
    assert [s["title"] for s in sections] == ["总体概览", "转诊闭环", "考核指标 followup_rate"]
    assert all("未配置取数口径" not in (s.get("note") or "") for s in sections), sections
    assert sections[2]["text"].startswith("随访完成率："), sections[2]


def test_一个段落都没勾_422(client, admin):
    resp = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P293_EMPTY", "name": "P293 空模板", "sections": []})
    assert resp.status_code == 422 and resp.json() == {"detail": "报告模板至少要有一个内容段落"}, resp.text
