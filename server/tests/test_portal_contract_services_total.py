"""居民端签约查询的「履约记录」标题不再把最近 20 次当总数印（P2-1550 的居民端那半，第四十五批扫描 AI1-9）。

修前：`portal.portal_my_contract` 每份协议只取最近 20 次履约（`limit(20)`，刻意的嵌套上限，见
`test_list_pagination_ratchet` 的 NESTED_CAP_FALSE_POSITIVES），出参不带总数；`m.js` 的 `renderContracts` 把这 20 条的条数
印成标题「履约记录（20）」。扫描实测（`r6_truncation.py`）：25 次履约的居民看到「（20）」，最早一条是第 6 次——前 5 次无处
可看，也不知道有（管理端履约记录是全量的「（25 次）」）。

修法：出参每份协议末尾加 `services_total`（该协议的履约总次数），`m.js` 列不全时标题写「履约记录：最近 20 次（共 N 次）」，
不足 20 次的照旧写次数。`services` 仍是最近 20 次，不改。
"""
import json
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PHONE = "13900015500"


@pytest.fixture(scope="module")
def world(client, admin):
    """一位居民在两家各签一份：甲镇记了 25 次履约，乙镇 3 次。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21550 居民", "id_card": "330102195401011550", "phone": PHONE})
    assert patient.status_code == 201, patient.text
    contracts = {}
    for key, name, times in (("a", "P21550 甲镇卫生院", 25), ("b", "P21550 乙镇卫生院", 3)):
        org = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
        signed = client.post("/api/contracts", headers=admin, json={
            "patient_id": patient.json()["id"], "org_id": org, "doctor_name": "P21550 家庭医生",
            "signed_date": "2026-01-01"})
        assert signed.status_code == 201, signed.text
        for i in range(times):
            resp = client.post(f"/api/contracts/{signed.json()['id']}/services", headers=admin,
                               json={"service_type": "followup", "note": f"{name} 第{i + 1}次"})
            assert resp.status_code == 201, resp.text
        contracts[key] = signed.json()["id"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
    resident = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code})
    assert resident.status_code == 200 and resident.json()["bound"], resident.text
    return {"contracts": contracts, "resident": {"Authorization": f"Bearer {resident.json()['access_token']}"}}


def _mine(client, world):
    resp = client.get("/api/portal/me/contract", headers=world["resident"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_每份协议带履约总次数_最近20次照旧(client, world):
    rows = {r["id"]: r for r in _mine(client, world)}
    many, few = rows[world["contracts"]["a"]], rows[world["contracts"]["b"]]
    assert (len(many["services"]), many["services_total"]) == (20, 25)   # 修前没有 services_total
    assert many["services"][-1]["note"] == "P21550 甲镇卫生院 第6次"     # services 仍是最近 20 次
    assert (len(few["services"]), few["services_total"]) == (3, 3)
    assert list(many)[-1] == "services_total"   # 新字段只加在末尾


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，常量取这一行。"""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    return source[start:source.index("\n", start) + 1]


def test_居民端标题写最近20次与总次数_不足20次照旧(client, world):
    """`m.js` 的 `renderContracts` 原文放进 node 跑，`authApi` 回放真接口（居民本人）的这份响应。"""
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    script = (
        "globalThis.document = { addEventListener() {}, cookie: '', querySelector() { return null; } };\n"
        + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "const DATA = JSON.parse(process.argv[1]);\nlet viewingPatientId = null;\n"
        + "async function authApi(path) { return DATA[path]; }\n"
        + "".join(_top(source, head) for head in ("const PACKAGES = ", "const SERVICE_TYPES = ", "function kv(",
                                                  "function svcQuery(", "async function renderContracts("))
        + "const box = { innerHTML: '' };\n"
        + "renderContracts(box).then(() => process.stdout.write(JSON.stringify(\n"
        + "  [...box.innerHTML.matchAll(/<div class=\"sec-title\">([^<]*)<\\/div>/g)].map((m) => m[1]))));\n"
    )
    data = {"/api/portal/me/contract": _mine(client, world)}
    done = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    # 两份协议按编号倒序：乙镇（3 次）在前、甲镇（25 次）在后
    assert json.loads(done.stdout) == ["履约记录（3）", "履约记录：最近 20 次（共 25 次）"]   # 修前甲镇那份印「履约记录（20）」
