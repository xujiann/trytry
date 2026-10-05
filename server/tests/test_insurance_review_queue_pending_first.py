"""医保协同页的特病 / 双通道审核队列：待审的被已审的挤出页面，管理层审不到、申报人重报 409，永远卡住（P2-1478，
第四十三批扫描 AG3-1）。

修前 `renderInsurance` 两张队列只取不带 `status` 的 `/api/insurance/special-diseases`、`/api/insurance/dual-channel`——
接口按 id 倒序只回全部状态里最新的 200 条（`list_special_diseases` / `list_dual_channel` 的 `limit(200)`）。扫描实测：先报
1 条特病、1 条双通道（待审），再报并批准 200 条，页面取到的两张队列各 200 行，「含待审: 0，第一条在不在: False」；同病种 /
同药品只许挂一条待审（部分唯一索引），申报人重报 409；只有直接调 `?status=applied` 查得到那条。

修法同 P2-1310 / P2-1441：待审的另取一页（特病 `?status=applied`、双通道 `?status=pending`），`actionableFirst` 排在最前、
按 id 去重；最新一页取满（清单不给总数）时标题写明「待审 N 条排在最前、其余只列最新 K 条」。接口不动。这里把
`renderInsurance` 原样拿到 node 里跑，`api` 经管道转给真接口。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import DualChannelApp, SpecialDiseaseApp
from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
SD_TITLE = "特病申报队列"
DUAL_TITLE = "双通道药品申报（医师/经办申报 → 管理层审核）"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 医保页取数换成桩：`$()` 给记 innerHTML 的假元素，`api()` 经管道转给真接口；角色按管理层（审核按钮只给它）
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", style: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem(key) { return key === "medplat_role" ? "director" : null; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function postAction() {}
function formJson() { return {}; }
function route() {}
async function spdModal() { return null; }
"""


def _render(client, headers) -> dict:
    core, clinical = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(")
                        for name in ("table", "panel", "actionableFirst", "setMsg", "currentRole"))
              + _top_level(clinical, "async function renderInsurance(")
              + "\n(async () => { await renderInsurance();\n"
                "  process.stdout.write(JSON.stringify({ result: { body: els['#page-body'].innerHTML, requested } }) + '\\n');\n"
                "  rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _queue(body: str, title: str) -> tuple[str, list[list[str]]]:
    """标题以 `title` 开头的那个面板：(完整标题, [每行的各格])。"""
    start = body.index(f"<h3>{title}")
    full = body[start + len("<h3>"):body.index("</h3>", start)]
    table = body[start:body.index("</table>", start)]
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    return full, [cells for cells in rows if cells]


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21478 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P21478 甲镇卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    users = {}
    for username, role, org in (("p21478_doc", "doctor", town), ("p21478_dir", "director", county)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org})
        assert created.status_code == 201, created.text
        users[role] = created.json()["id"]
    doctor, director = login(client, "p21478_doc", "passw0rd1"), login(client, "p21478_dir", "passw0rd1")
    patients = [client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card}).json()["id"]
                for name, id_card in (("P21478 张三", "330102197001011478"), ("P21478 李四", "330102197002021474"))]
    first_sd = client.post("/api/insurance/special-diseases", headers=doctor,
                           json={"patient_id": patients[0], "disease_name": "P21478 尿毒症透析"})
    first_dual = client.post("/api/insurance/dual-channel", headers=doctor,
                             json={"patient_id": patients[0], "drug_name": "P21478 司美格鲁肽注射液"})
    assert first_sd.status_code == first_dual.status_code == 201, (first_sd.text, first_dual.text)
    return {"director": director, "doctor": doctor, "users": users, "patients": patients,
            "sd": first_sd.json()["id"], "dual": first_dual.json()["id"]}


def _flood(world, n: int = 200) -> None:
    """再报并批准 n 条（逐个走接口太慢，直接落库已批准的）。"""
    who = {"created_by": world["users"]["doctor"], "reviewed_by": world["users"]["director"]}
    with SessionLocal() as db:
        db.add_all([SpecialDiseaseApp(patient_id=world["patients"][1], disease_name=f"P21478 病种{i}",
                                      status="approved", **who) for i in range(n)])
        db.add_all([DualChannelApp(patient_id=world["patients"][1], drug_name=f"P21478 药品{i}",
                                   status="approved", **who) for i in range(n)])
        db.commit()


def test_列得全时待审照样排在最前_标题不写截断(client, world):
    out = _render(client, world["director"])
    for title, first, button in ((SD_TITLE, world["sd"], "data-ok"), (DUAL_TITLE, world["dual"], "data-dualok")):
        full, rows = _queue(out["body"], title)
        assert full == title
        assert len(rows) == 1 and rows[0][0] == str(first) and f'{button}="{first}"' in rows[0][-1]
    assert "/api/insurance/special-diseases?status=applied" in out["requested"]
    assert "/api/insurance/dual-channel?status=pending" in out["requested"]


def test_1条待审加200条已审_两张队列都含那条待审_排在最前_标题写明截断(client, world):
    _flood(world)
    director = world["director"]
    # 缺省一页（页面修前就是这么取的）取不到那条待审
    assert world["sd"] not in [a["id"] for a in client.get("/api/insurance/special-diseases", headers=director).json()]
    assert world["dual"] not in [a["id"] for a in client.get("/api/insurance/dual-channel", headers=director).json()]

    out = _render(client, director)
    for title, first, button in ((SD_TITLE, world["sd"], "data-ok"), (DUAL_TITLE, world["dual"], "data-dualok")):
        full, rows = _queue(out["body"], title)
        # 修前 200 行、第一条（待审）不在：管理层没有一行能批
        assert rows[0][0] == str(first), (title, rows[:2])
        assert f'{button}="{first}"' in rows[0][-1]
        assert len(rows) == 201 and str(first) not in [cells[0] for cells in rows[1:]]   # 按 id 去重
        assert full == f"{title}（待审 1 条排在最前、其余只列最新 200 条）"

    # 申报人重报照旧 409（部分唯一索引只锁待审那一条）——所以那条必须审得到
    again = client.post("/api/insurance/special-diseases", headers=world["doctor"],
                        json={"patient_id": world["patients"][0], "disease_name": "P21478 尿毒症透析"})
    assert again.status_code == 409, again.text
