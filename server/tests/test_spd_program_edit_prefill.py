"""运行中枢「编辑」「改规则」按点击这一刻的病种预填（P2-618，读动词孤儿 `GET /api/spd/programs/{id}` 接入口）。

「编辑」的说明框原先不预填——框恒空，看不到原来写的什么，改一个字要整段重敲，空着又不送、清不掉；名称 / 科室
与「改规则」的编辑器摆的是进页面时那份列表，其间别人改过的（改名、改规则升了版），这里一保存就改回去（规则的那一版
只剩在版本快照里）。现在点的时候先按接口取这个病种，说明照实预填、清空即清掉。
"""
from pathlib import Path

import pytest

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def program(client, admin):
    made = client.post(f"{B}/programs", headers=admin, json={
        "code": "P2618", "name": "P2618 预填病种", "lead_dept": "内科", "description": "原说明"})
    assert made.status_code == 201, made.text
    return made.json()


def test_详情带说明_页面据此预填(client, admin, program):
    got = client.get(f"{B}/programs/{program['id']}", headers=admin)
    assert got.status_code == 200, got.text
    assert (got.json()["name"], got.json()["lead_dept"], got.json()["description"]) == ("P2618 预填病种", "内科", "原说明")


def test_照原样送回不升版_清空说明即清掉(client, admin, program):
    same = client.patch(f"{B}/programs/{program['id']}", headers=admin, json={
        "name": "P2618 预填病种", "lead_dept": "内科", "description": "原说明"})
    assert same.status_code == 200 and same.json()["version"] == program["version"], same.text   # 只改说明类字段不升版
    cleared = client.patch(f"{B}/programs/{program['id']}", headers=admin, json={
        "name": "P2618 预填病种", "lead_dept": "内科", "description": ""})
    assert cleared.status_code == 200 and cleared.json()["description"] == "", cleared.text


def _handler(start, end):
    return PAGE[PAGE.index(start):PAGE.index(end, PAGE.index(start))]


def test_编辑先取这个病种_说明照实预填_清空照送():
    body = _handler("if (progEdit) {", "if (progRules) {")
    fetch = body.index("await api(`/api/spd/programs/${progEdit.dataset.progEdit}`)")
    assert fetch < body.index("spdModal(")   # 先取再开表单
    assert 'type: "textarea", value: cur.description' in body   # 修前说明框不预填
    assert 'description: form.description || ""' in body   # 修前 `if (form.description)`：空着不送、清不掉
    assert "progEdit.dataset.name" not in body and "progEdit.dataset.dept" not in body   # 不再用进页面时那份


def test_改规则取点击这一刻的规则():
    body = _handler("if (progRules) {", "if (progVersions) {")
    assert "await api(`/api/spd/programs/${progRules.dataset.progRules}`)" in body
    assert "programs.find(" not in body   # 修前摆的是进页面时那份列表里的规则
