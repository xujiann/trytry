"""孕产妇建册的末次月经 / 预产期不校验日期：「2026/1/5」「2026-02-30」照存（P2-231）。

`MaternalCreate.lmp` / `edc` 是裸 `str`（只卡长度 10）。请求体日期闸门（`test_datestr_single_source.py`）按字段名认
`date` / `due`，这两个按产科惯例用缩写命名的日期不在它视野里，于是 P1-61 那一轮把 22 处日期换成 `DateStr` 时漏了它俩。
页面上是两个自由文本框（「末次月经 YYYY-MM-DD」），写成斜杠、两位年份、不存在的日子一律 201 照存；预产期还印在
档案清单上，按字符串排序与比较的地方都会把它排乱。

修法：两处改 `OptionalDateStr`（可留空，与原先一样），出参覆盖回 `str`（P1-63：存量坏值原样读出，不让清单 500）；
闸门的词元补上 `lmp` / `edc`。
"""
import pytest

B = "/api/maternal/records"


@pytest.fixture(scope="module")
def patient(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P2231 孕妇", "id_card": "330127199202022231", "gender": "女", "birth_date": "1992-02-02"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize("field", ["lmp", "edc"])
@pytest.mark.parametrize("bad", ["2026/1/5", "26-01-05", "2026-02-30", "２０２６-01-05"])
def test_写错的末次月经与预产期_422(client, admin, patient, field, bad):
    resp = client.post(B, headers=admin, json={"patient_id": patient, field: bad})
    assert resp.status_code == 422, (field, bad, resp.status_code, resp.text[:200])   # 修前 201 照存
    assert any(e["loc"] == ["body", field] for e in resp.json()["detail"]), resp.text


def test_合法日期与留空照收_存量坏值照旧读得出(client, admin, patient):
    from app.database import SessionLocal
    from app.models import MaternalRecord

    ok = client.post(B, headers=admin, json={"patient_id": patient, "lmp": "2026-06-01", "edc": ""})
    assert ok.status_code == 201 and (ok.json()["lmp"], ok.json()["edc"]) == ("2026-06-01", ""), ok.text
    with SessionLocal() as db:   # 修之前存进去的坏值
        legacy = MaternalRecord(patient_id=patient, lmp="2025/1/5", edc="2025-10-1", status="closed")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    rows = client.get(f"{B}?patient_id={patient}", headers=admin)
    assert rows.status_code == 200, rows.text
    assert {(r["lmp"], r["edc"]) for r in rows.json() if r["id"] == legacy_id} == {("2025/1/5", "2025-10-1")}
