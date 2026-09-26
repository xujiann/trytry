"""审方规则的「特殊人群」只收 pregnant / child / elderly（P2-198）。

`DrugRuleCreate.special_groups` 的注释写「取值 pregnant/child/elderly」，却是任意文本：导入
`{"special_groups": "孕产妇,老年人"}`（正是页面上印给人看的中文名）照样 200，规则表里也照这么显示——审方按英文键
匹配患者所属人群，这条规则一个患者都命中不了，80 岁老人的华法林照样系统审通过，不报错。P1-121 同形（闭集照 422）。
"""
import pytest


def test_中文名或拼错的人群_建规则与导入都422(client, admin):
    bad = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": "P2198-WARF", "max_daily_dose": 10, "special_groups": "孕产妇,老年人"})
    assert bad.status_code == 422, bad.text                       # 修前 201
    assert "特殊人群只认" in bad.text and "孕产妇" in bad.text
    imported = client.post("/api/prescriptions/rules/import", headers=admin, json=[
        {"drug_code": "P2198-LEVO", "max_daily_dose": 500, "special_groups": "children"}])
    assert imported.status_code == 422, imported.text             # 修前 200


@pytest.mark.parametrize("groups", ["", "pregnant", "child,elderly", "pregnant， elderly"])
def test_合法取值照收(client, admin, groups):
    resp = client.post("/api/prescriptions/rules/import", headers=admin, json=[
        {"drug_code": "P2198-OK", "max_daily_dose": 10, "special_groups": groups}])
    assert resp.status_code == 200, resp.text
