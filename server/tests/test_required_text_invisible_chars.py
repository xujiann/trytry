"""必填文本不收只有看不见的字符的值：纯零宽空格、BOM、词连接符、控制字符与纯空白一样 422（P2-1148，第三十三批扫描 A3-7）。

`texttypes.NON_BLANK` 原先是 `\\S`，只挡空白：pydantic 用的 Rust 正则里 `\\s` 指 Unicode White_Space，U+200B、U+FEFF、
U+2060、\\x00、\\x01 都不算空白。2026-09-30 开发库实测：患者姓名 "\\u200b"、"\\ufeff"、"\\x01" 都 201，机构名两个 U+200B
201——清单里多出看不见名字的档案和机构，正是 P1-109 要防的；处方明细的药品编码只有一个 U+200B、华法林 50mg 201
auto_passed，P1-110 的「编码必填」落空。数据质控的「为空」（`dataquality._is_blank`）按 `strip()` 判，同样查不出来。

修法：`NON_BLANK` 改成要求至少一个「非空白、非控制字符（Cc）、非格式字符（Cf）」的字符（`[^\\s\\p{Cc}\\p{Cf}]`）；
422 仍是同一个 `string_pattern_mismatch`，前端 `errorText` 认新 pattern，文案与纯空白同一句「不能只填空格」；数据质控的
「为空」用 Python 侧同一个判据 `texttypes.is_blank_text`。带零宽字符但有看得见的字的照原样收、原样落库。
"""
import json
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import SchemaValidator, core_schema

from app.texttypes import NON_BLANK, is_blank_text

INVISIBLE = ["\u200b", "\u200b\u200b", "\ufeff", "\u2060", "\u00ad", "\u200e\u200f", "\x00", "\x01", "\x7f",
             " \u200b　\t"]
INVISIBLE_IDS = ["零宽空格", "两个零宽空格", "BOM", "词连接符", "软连字符", "左右向标记", "NUL", "控制字符01", "DEL",
                 "空白夹零宽"]
VISIBLE = ["张\u200b三", "\ufeff李四", "王五\u2060", "A\x01", "甲", " 甲乡卫生院 ", "　张三"]


class _Probe(BaseModel):
    name: str = Field(min_length=1, max_length=16, pattern=NON_BLANK)


@pytest.mark.parametrize("value", INVISIBLE, ids=INVISIBLE_IDS)
def test_只有看不见的字符_与纯空白同一个422(value):
    with pytest.raises(ValidationError) as exc:
        _Probe(name=value)   # 修前照收
    (error,) = exc.value.errors()
    assert (error["type"], error["ctx"]) == ("string_pattern_mismatch", {"pattern": NON_BLANK})
    assert is_blank_text(value)


@pytest.mark.parametrize("value", VISIBLE)
def test_有看得见的字_照原样收(value):
    assert _Probe(name=value).name == value
    assert not is_blank_text(value)


def test_Python侧判据与请求校验的正则逐码点一致():
    """`is_blank_text` 是 `NON_BLANK` 的 Python 版（`\\p{..}` Python 的 `re` 不认），两边不许各算各的。
    只比 Python 的 Unicode 数据库里已分配的码点：Rust 正则库的 Unicode 版本可能更新（新分配的 Cf 这边还是未分配）。"""
    validator = SchemaValidator(core_schema.str_schema(pattern=NON_BLANK))
    differ = [hex(cp) for cp in range(sys.maxunicode + 1)
              if not 0xD800 <= cp <= 0xDFFF and unicodedata.category(chr(cp)) != "Cn"
              and validator.isinstance_python(chr(cp)) == is_blank_text(chr(cp))]
    assert differ == []


# ================================================================ 端点（修前实测全部 201）
def _rejected(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    errors = [e for e in resp.json()["detail"] if e["loc"][-1] == field]
    assert errors and all((e["type"], e["ctx"]) == ("string_pattern_mismatch", {"pattern": NON_BLANK})
                          for e in errors), errors


@pytest.mark.parametrize("name", ["\u200b", "\ufeff", "\x01"], ids=["零宽空格", "BOM", "控制字符01"])
def test_患者姓名只有看不见的字符_422(client, admin, name):
    _rejected(client.post("/api/patients", headers=admin, json={
        "name": name, "id_card": "110101199001011253", "gender": "男", "birth_date": "1990-01-01"}), "name")


def test_机构名只有零宽空格_422_带零宽的正常名字照原样落库(client, admin):
    _rejected(client.post("/api/organizations", headers=admin, json={
        "name": "\u200b\u200b", "org_type": "township", "level": "township"}), "name")
    ok = client.post("/api/organizations", headers=admin, json={
        "name": "P21148\u200b甲乡卫生院", "org_type": "township", "level": "township"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["name"] == "P21148\u200b甲乡卫生院"


def test_处方药品编码只有零宽空格_422(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21148 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21148 患者", "id_card": "330102197001011148"}).json()["id"]
    resp = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "心房颤动",
        "items": [{"drug_code": "\u200b", "drug_name": "华法林钠片", "daily_dose": 50}]})
    _rejected(resp, "drug_code")   # 修前 201 auto_passed：没有规则对得上


def test_数据质控的为空_只有看不见的字符也算(client, admin):
    """HL7 / FHIR 入站与存量不经过请求模型：这类姓名进得了库，质控 QC002「患者姓名不得为空」要点得出来。"""
    from app.database import SessionLocal
    from app.models import Patient
    from app.routers.dataquality import _is_blank

    assert _is_blank("\u200b") and _is_blank("\ufeff \x01") and _is_blank(None) and _is_blank("   ")
    assert not _is_blank("张\u200b三") and not _is_blank(0)
    with SessionLocal() as db:
        stock = Patient(ehc_no="P21148-QC", name="\u200b", id_card="110101199001011270", gender="男")
        db.add(stock)
        db.commit()
        stock_id = stock.id
    run = client.get("/api/dataquality/run", headers=admin, params={"rule_code": "QC002", "limit": 1000})
    assert run.status_code == 200, run.text
    assert stock_id in {v["record_id"] for v in run.json()["items"]}   # 修前查不出来


# ================================================================ 前端：文案与纯空白同一句
SHARED = (Path(__file__).resolve().parents[1] / "app" / "static" / "shared.js").read_text(encoding="utf-8")


def _render(detail) -> str:
    start = SHARED.index("function errorText(detail, fallback) {")
    script = SHARED[start:SHARED.index("\n}\n", start) + 2]
    script += "\nconsole.log(errorText(JSON.parse(process.argv[1]), '兜底'));"
    return subprocess.run(["node", "-e", script, json.dumps(detail)], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
@pytest.mark.parametrize("value", ["   ", "\u200b", "\ufeff", "\x01"], ids=["纯空白", "零宽空格", "BOM", "控制字符01"])
def test_前端报错文案与纯空白同一句(value):
    with pytest.raises(ValidationError) as exc:
        _Probe(name=value)
    assert _render(json.loads(exc.value.json())) == "name：不能只填空格"
