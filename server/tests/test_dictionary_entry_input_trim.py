"""统一编码字典的 HTTP 入口去首尾空白、不收科学计数法，与 CLI 导入同一口径（P2-1734，第五十一批扫描 AO3-3）。

修前：CLI `scripts/import_dictionary.py` 对编码、名称去首尾空白，编码 / 医保对码 / 本位码是科学计数法的记错误行（P2-1125）；
HTTP 入口（单条新增 `CodeEntryCreate`、批量导入 `CodeEntryUpsert`）两样都不做，只挡纯空白。扫描实测（`r1_dict.py`）：
- 种子里已有 I10，再建 `' I10'`、`'I10 '` 都 201，页面上与 I10 看着一模一样；
- 收费字典导入 `"310300001 "` 后，按干净编码 `310300001` 建收费项目 422「编码不在四统一收费字典内」，带空格的反倒 201；
- `"8.69E+13"` 作编码、作本位码照收。

修法：入参对编码、名称与各属性列去首尾空白（照 CLI 的 `str.strip()`），编码、医保对码、本位码命中
`texttypes.excel_sci_notation`（CLI 用的同一个函数）回 422；出参覆盖回不带这两样的声明，存量照原样读出。
大小写 / 全角归一不在本条（随 P2-785）。
"""
import pytest

from app.database import SessionLocal
from app.models import CodeEntry, CodeSystem


def _rows(system: str, *codes: str) -> list[tuple]:
    with SessionLocal() as db:
        system_id = db.query(CodeSystem).filter(CodeSystem.code == system).one().id
        return [(e.code, e.name, e.spec, e.unit, e.insurance_code, e.national_code)
                for e in db.query(CodeEntry).filter(CodeEntry.system_id == system_id, CodeEntry.code.in_(codes))
                .order_by(CodeEntry.code)]


@pytest.mark.parametrize("code", ["I10 ", " I10", "\tI10\u3000"])
def test_单条新增去首尾空白_撞上种子里的I10就409(client, admin, code):
    assert _rows("diagnosis", "I10"), "前提：启动种子里有 I10"
    resp = client.post("/api/dictionaries/diagnosis/entries", headers=admin, json={"code": code, "name": "原发性高血压"})
    assert resp.status_code == 409, resp.text   # 修前 201：与 I10 并存、页面上看着一模一样
    assert resp.json() == {"detail": "编码已存在"}
    assert _rows("diagnosis", code) == []


def test_批量导入去首尾空白_按干净编码建收费项目照收(client, admin):
    resp = client.post("/api/dictionaries/charge/import", headers=admin, json=[
        {"code": "310300001 ", "name": "心电图 ", "unit": " 次 ", "spec": "\u3000常规十二导联 "}])
    assert resp.status_code == 200 and resp.json()["imported"] == 1, resp.text
    assert _rows("charge", "310300001", "310300001 ") == [("310300001", "心电图", "常规十二导联", "次", None, None)]
    # 收费字典非空即管控：按干净编码建收费项目，修前 422「编码不在四统一收费字典内」
    item = client.post("/api/billing/charge-items", headers=admin, json={
        "code": "310300001", "name": "心电图", "category": "exam", "price": 25})
    assert item.status_code == 201, item.text
    # 再导一遍干净的：是「库里已有」，不再多出一个孪生条目
    again = client.post("/api/dictionaries/charge/import", headers=admin, json=[{"code": "310300001", "name": "心电图"}])
    assert again.json()["skipped_existing"] == 1 and again.json()["imported"] == 0


@pytest.mark.parametrize("entry, field", [
    ({"code": "8.69E+13", "name": "某药"}, "code"),
    ({"code": "P21734-SCI-1", "name": "某药", "national_code": "8.69E+13"}, "national_code"),
    ({"code": "P21734-SCI-2", "name": "某药", "insurance_code": " 1.10101E+17 "}, "insurance_code"),
])
def test_编码医保码本位码是科学计数法的422(client, admin, entry, field):
    single = client.post("/api/dictionaries/drug/entries", headers=admin, json=entry)
    assert single.status_code == 422, single.text   # 修前 201
    (error,) = single.json()["detail"]
    assert error["loc"] == ["body", field] and "科学计数法" in error["msg"], error
    # 批量导入整批 422、一条不落（同其他字段校验不过）
    batch = client.post("/api/dictionaries/drug/import", headers=admin, json=[{"code": "P21734-OK-0", "name": "好药"}, entry])
    assert batch.status_code == 422, batch.text
    assert [e["loc"] for e in batch.json()["detail"]] == [["body", 1, field]]
    assert _rows("drug", "P21734-OK-0", entry["code"]) == []


def test_正常条目照收_中间的空格与长数字本位码原样(client, admin):
    resp = client.post("/api/dictionaries/drug/entries", headers=admin, json={
        "code": "P21734 A10BA02", "name": "二甲双胍 片", "spec": "0.5g×24片", "unit": "盒",
        "insurance_code": "XA10BAE054A001010100001", "national_code": "86900000000035"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["code"], body["name"], body["national_code"]) == ("P21734 A10BA02", "二甲双胍 片", "86900000000035")
    imported = client.post("/api/dictionaries/drug/import", headers=admin, json=[
        {"code": "P21734-IMP", "name": "导入药", "manufacturer": "某厂", "extra": '{"k": 1}'}])
    assert imported.json()["imported"] == 1, imported.text


def test_出参原样读出存量的带空白与科学计数法条目(client, admin):
    """出参不带入参的去空白与科学计数法校验：修之前存进去的条目照原样读出，不被改写、不让清单 500。"""
    with SessionLocal() as db:
        system_id = db.query(CodeSystem).filter(CodeSystem.code == "consumable").one().id
        db.add(CodeEntry(system_id=system_id, code="P21734-OLD ", name=" 旧耗材 ", national_code="8.69E+13"))
        db.commit()
    resp = client.get("/api/dictionaries/consumable/entries", headers=admin, params={"keyword": "P21734-OLD"})
    assert resp.status_code == 200, resp.text
    assert [(e["code"], e["name"], e["national_code"]) for e in resp.json()] == [("P21734-OLD ", " 旧耗材 ", "8.69E+13")]
