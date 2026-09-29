"""复诊、随访看板的「只看逾期 / 超期」不再与状态相与（P2-828，第二十二批「页面查询参数 vs 后端」扫描 X4-8）。

后端 `overdue=true` 先扫一遍超期、再按 `status == 'overdue'` 筛，与传入的 status 相与（`care.list_revisits` /
`followup.list_followup_records`）；页面却把状态下拉与「只看逾期 / 超期」摆成两个能同时选的独立条件——「已排期 + 只看
逾期」「待随访 + 只看超期」恒为空，而这一步扫描刚把那条记录改成了超期，看的人会以为没有超期的。修后勾上就把状态置灰、
不送。
"""
from pathlib import Path

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_接口口径_逾期与状态相与(client, admin):
    """钉住页面所依赖的接口口径：只带 overdue 取得到逾期的，再带 planned 是空的——所以页面勾了就不能再送状态。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2828 患者", "id_card": "330106196802022828"}).json()["id"]
    made = client.post(f"{B}/revisits", headers=admin, json={"patient_id": patient, "plan_date": "2020-01-01"})
    assert made.status_code == 201, made.text

    def ids(**params):
        rows = client.get(f"{B}/revisits", headers=admin, params={"patient_id": patient, "limit": 50, **params})
        assert rows.status_code == 200, rows.text
        return [(r["id"], r["status"]) for r in rows.json()]

    assert ids(overdue="true") == [(made.json()["id"], "overdue")]
    assert ids(overdue="true", status="planned") == []


def test_页面勾了逾期就把状态置灰不送():
    assert "fuFilter.overdue.onchange = () => { fuFilter.status.disabled = fuFilter.overdue.checked; };" in PAGE
    assert "revisitFilter.overdue.onchange = () => { revisitFilter.status.disabled = revisitFilter.overdue.checked; };" in PAGE
    assert 'const status = e.target.overdue.checked ? "" : e.target.status.value;' in PAGE   # 修前两个一起送
