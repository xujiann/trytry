"""同意书打印件与居民端把监护关系印成编码「parent」（P2-238）。

居民端签署同意书的「监护关系」下拉送的是编码 `parent` / `guardian`（界面上是「父母 / 其他监护人」），接口原样落库；
打印件「与患者关系」一栏、居民端的同意记录都原样印出编码——家长拿到的纸面同意书上写着「与患者关系：parent」。
平台的状态与类型文案一律取自后端（§13），这一处漏了。

修法：`consents.GUARDIAN_RELATION_NAMES` 一份字典，出参加 `guardian_relation_name`（只加字段，自由文本原样），
打印件与居民端都取它。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture(scope="module")
def minor(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P2238 幼童", "id_card": "330782202001012238", "birth_date": "2020-01-01"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _sign(client, admin, patient, relation):
    resp = client.post("/api/consents", headers=admin, json={
        "patient_id": patient, "scene": "archive", "evidence": "签字影像#P2238",
        "guardian_name": "P2238 家长", "guardian_id_card": "330782199001012238", "guardian_relation": relation})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("relation, name", [("parent", "父母"), ("guardian", "其他监护人"), ("母亲", "母亲")])
def test_监护关系出参带中文_打印件印中文(client, admin, minor, relation, name):
    record = _sign(client, admin, minor, relation)
    assert (record["guardian_relation"], record["guardian_relation_name"]) == (relation, name)
    page = client.get(f"/api/print/consents/{record['id']}", headers=admin)
    assert page.status_code == 200, page.text
    assert f'<td class="k">与患者关系</td><td>{name}</td>' in page.text   # 修前印 parent / guardian


def test_居民端同意记录取后端给的中文():
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    assert "c.guardian_relation_name" in source
    assert 'esc(c.guardian_relation || "监护人")' not in source
