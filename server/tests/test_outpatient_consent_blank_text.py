"""不用模板开告知书，标题 / 正文纯空白照收：存进一张空白告知书让患者签（P2-248）。

`POST /api/outpatient/consents` 不选模板时须自带标题与正文（「未选用模板时须自带标题与正文」）；两个字段因为用模板时
可以不给，没法在字段上挂 `NON_BLANK`，路由里只判了 `not title or not content`——「   」是真值，过了。模板那一侧早在
P1-98 就挡了空白（「患者签的是一份空白同意书」）。用模板时空白标题还顶掉了模板的标题（`title or template.title`）。

修法：纯空白当没给——不用模板的 422，用模板的回落到模板标题。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2248 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2248 患者", "id_card": "330106198909092248"}).json()["id"]
    template = client.post("/api/outpatient/consent-templates", headers=admin, json={
        "consent_type": "exam", "title": "增强 CT 检查知情告知书", "body": "造影剂可能引起过敏反应……"})
    assert template.status_code == 201, template.text
    return {"org": org, "patient": patient, "template": template.json()["id"]}


def _consent(client, admin, world, **fields):
    return client.post("/api/outpatient/consents", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "consent_type": "exam", **fields})


@pytest.mark.parametrize("fields", [
    {"title": "   ", "content": "检查可能出现不适……"},
    {"title": "胃镜检查知情告知", "content": " \n\t "},
    {"title": "　", "content": "　　"},
], ids=["标题空白", "正文空白", "全角空格"])
def test_不用模板时空白标题或正文_422(client, admin, world, fields):
    resp = _consent(client, admin, world, **fields)
    assert resp.status_code == 422, resp.text   # 修前 201：存进一张空白告知书
    assert "须自带标题与正文" in resp.json()["detail"]


def test_用模板时空白标题回落到模板标题(client, admin, world):
    resp = _consent(client, admin, world, template_id=world["template"], title="  ")
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "增强 CT 检查知情告知书"   # 修前「  」顶掉了模板标题


def test_正常的照旧收_原样存不裁空白(client, admin, world):
    resp = _consent(client, admin, world, title=" 胃镜检查知情告知 ", content="胃镜检查可能出现咽部不适……\n")
    assert resp.status_code == 201, resp.text
    assert (resp.json()["title"], resp.json()["content"]) == (" 胃镜检查知情告知 ", "胃镜检查可能出现咽部不适……\n")
