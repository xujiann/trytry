"""基金池的状态文案取自后端、不在执行中的池子不给预付 / 预结 / 清算表单（P2-598，第十二批「按钮 vs 状态机」扫描 Z2-8）。

页面原先自带一份状态文案「在用 / 已关闭」，模型列注释与 409 报错说的是「执行中 / 已归档」——同一个池子，清单上写着
「已关闭」，点预付报「基金池状态为 已归档，不可再预付」。打开一个已归档 / 已清算的池子，预付、预结、清算三张表单照样
摆着，填完点下去才 409。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pool(client, admin):
    created = client.post("/api/fund/pools", headers=admin, json={
        "year": 2097, "insurance_type": "resident", "total_amount": 500000, "prepay_ratio_pct": 60})
    assert created.status_code == 201, created.text
    return created.json()


def test_清单带状态文案_与报错同一个说法(client, admin, pool):
    assert pool["status_name"] == "执行中"   # 修前没有这一键，页面自己译成「在用」
    closed = client.patch(f"/api/fund/pools/{pool['id']}", headers=admin, json={"status": "closed"})
    assert closed.status_code == 200 and closed.json()["status_name"] == "已归档", closed.text
    (row,) = [p for p in client.get("/api/fund/pools", headers=admin).json() if p["id"] == pool["id"]]
    assert row["status_name"] == "已归档"
    for path, body in (("prepayments", {"amount": 1000}), ("periods", {"period": "2097-01", "actual_amount": 10})):
        got = client.post(f"/api/fund/pools/{pool['id']}/{path}", headers=admin, json=body)
        assert got.status_code == 409 and "已归档" in got.json()["detail"], got.text


def test_页面状态取后端文案_不在执行中的池子不给三张表单():
    start = PAGE.index("async function renderFund() {")
    body = PAGE[start:PAGE.index("\n}\n", start)]
    assert "esc(p.status_name)" in body
    assert "在用" not in body and "已关闭" not in body   # 修前清单与编辑框各一处
    assert '${poolOpen ? `<form class="inline" id="fd-prepay">' in body
    assert '${poolOpen ? `<form class="inline" id="fd-period">' in body
    assert '!poolOpen ? notOpen("清算")' in body
    assert "if (picked && poolOpen) {" in body   # 表单不在就别挂提交事件
