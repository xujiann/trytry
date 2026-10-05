"""用血申请队列让审批人、发血经办看得到判断依据（P2-1469，第四十三批扫描 AG1-3 + AG4-7）。

修前申请清单 `GET /api/blood/requests` 每行只有七个键（ID / 患者号 / 机构号 / 血型 / 成分编码 / 毫升 / 状态）：表单收了
「用血原因」、库里也存了，却没有一个接口返回，审批人和发血经办看不到用血原因、申请人、申请时间和患者姓名，就要点批准、
点发血；统一申请单里用血行的标题印成分编码「A rbc 400ml」，成分文案表只在前端一份（`pages-public.js` 的 `BLOOD_COMPONENTS`）。

修法：`TransfusionRowOut` 末尾只增五键——`reason`、`requested_by_name`（申请人 `full_name or username`）、`created_at`、
`patient_name`、`component_name`，原有七键与次序不动；姓名按本页一次 IN 取（`deps.rows_by_id`，不逐行 `db.get`），可见范围
仍是 `scope_org_list`。成分文案在 `blood.py` 定一份（`COMPONENT_NAMES`），统一申请单用血行的标题也用它（「A 红细胞 400ml」）。
页面队列加「申请人 / 申请时间 / 用血原因」三列，患者列印姓名带编号，成分列印后端给的 `component_name`；前端
`BLOOD_COMPONENTS` 仍给表单下拉用，这里钉住它与后端那份一致。页面函数原样拿到 node 里跑（夹具见 `tests/blood_page.py`）。
"""
import json
import re
import shutil
import subprocess
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from blood_page import STATIC, render, responses, top_const
from conftest import login

from app.database import SessionLocal, engine
from app.models import TransfusionRequest

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

OLD_KEYS = ["id", "patient_id", "org_id", "blood_type", "component", "quantity_ml", "status"]
NEW_KEYS = ["reason", "requested_by_name", "created_at", "patient_name", "component_name"]
PATIENT_NAME = "P21469 钱<二>"   # 带尖括号：页面上必须转义


@contextmanager
def count_sql():
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def _user(client, admin, username, role, org, full_name=""):
    created = client.post("/api/users", headers=admin, json={
        "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": full_name})
    assert created.status_code in (200, 201), created.text
    return login(client, username, "passw0rd1")


def _patient(client, admin, name, n):
    made = client.post("/api/patients", headers=admin, json={"name": name, "id_card": f"33012719730405{n:04d}"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


@pytest.fixture(scope="module")
def world(client, admin):
    """县医院：张医生（填了姓名）、另一位医师（没填姓名）各提一张申请，管理层审批、血库经办发血；乙县医院的经办看不到它们。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21469 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P21469 乙县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    doctor = _user(client, admin, "p21469_doc", "doctor", org, "张医生")
    nameless = _user(client, admin, "p21469_doc2", "doctor", org)
    director = _user(client, admin, "p21469_dir", "director", org, "李主任")
    operator = _user(client, admin, "p21469_op", "operator", org, "孙经办")
    stranger = _user(client, admin, "p21469_other", "operator", other, "王经办")
    patients = [_patient(client, admin, "P21469 赵一", 1), _patient(client, admin, PATIENT_NAME, 2)]
    made = []
    for headers, patient, body in (
            (doctor, patients[0], {"blood_type": "A", "component": "rbc", "quantity_ml": 400,
                                   "reason": "术中失血 Hb 62g/L <急>"}),
            (nameless, patients[1], {"blood_type": "B", "component": "plasma", "quantity_ml": 200})):
        resp = client.post("/api/blood/requests", headers=headers, json={"patient_id": patient, "org_id": org, **body})
        assert resp.status_code == 201, resp.text
        made.append(resp.json()["id"])
    return {"org": org, "patients": patients, "requests": made, "director": director, "operator": operator,
            "stranger": stranger, "doctor": doctor}


def _created_at(request_id):
    with SessionLocal() as db:
        return db.get(TransfusionRequest, request_id).created_at.isoformat()


@pytest.mark.parametrize("who", ["director", "operator"])
def test_审批人与发血经办取到的队列行带原因申请人申请时刻与姓名_原有七键不动(client, world, who):
    rows = client.get("/api/blood/requests", headers=world[who], params={"status": "pending"}).json()
    mine = [r for r in rows if r["id"] in world["requests"]]
    assert [list(r.keys()) for r in mine] == [OLD_KEYS + NEW_KEYS] * 2   # 修前只有前七个键
    second, first = mine   # id 倒序
    assert {k: first[k] for k in NEW_KEYS} == {
        "reason": "术中失血 Hb 62g/L <急>", "requested_by_name": "张医生", "created_at": _created_at(first["id"]),
        "patient_name": "P21469 赵一", "component_name": "红细胞"}
    assert {k: second[k] for k in NEW_KEYS} == {
        "reason": "", "requested_by_name": "p21469_doc2",   # 没填姓名的回落账号
        "created_at": _created_at(second["id"]), "patient_name": PATIENT_NAME, "component_name": "血浆"}
    assert {k: first[k] for k in OLD_KEYS} == {
        "id": world["requests"][0], "patient_id": world["patients"][0], "org_id": world["org"], "blood_type": "A",
        "component": "rbc", "quantity_ml": 400, "status": "pending"}


def test_可见范围不变_别家机构的经办仍看不到(client, world):
    rows = client.get("/api/blood/requests", headers=world["stranger"]).json()
    assert not {r["id"] for r in rows} & set(world["requests"])


def test_姓名按页一次取_SQL条数不随页大小涨(client, admin, world):
    """修法要求按本页一次 IN 取申请人与患者（rows_by_id），不逐行 db.get：页大小 2 与 12，SQL 条数一样。"""
    for n in range(12):
        patient = _patient(client, admin, f"P21469 批量{n}", 100 + n)
        resp = client.post("/api/blood/requests", headers=world["doctor"], json={
            "patient_id": patient, "org_id": world["org"], "blood_type": "O", "component": "platelet",
            "quantity_ml": 100})
        assert resp.status_code == 201, resp.text
    counts = []
    for limit in (2, 12):
        with count_sql() as counter:
            resp = client.get("/api/blood/requests", headers=world["director"], params={"limit": limit})
        assert resp.status_code == 200 and len(resp.json()) == limit, resp.text
        assert {r["patient_name"] for r in resp.json()} <= {f"P21469 批量{n}" for n in range(12)}
        counts.append(counter["n"])
    assert counts[0] == counts[1], counts


def test_统一申请单用血行标题写中文成分(client, admin, world):
    got = client.get("/api/service-requests", headers=admin, params={
        "patient_id": world["patients"][0], "request_type": "blood"}).json()
    assert [i["title"] for i in got["items"]] == ["A 红细胞 400ml"]   # 修前「A rbc 400ml」


def test_成分文案前后端一份口径():
    """前端 `BLOOD_COMPONENTS` 仍给表单下拉与库存台账用，与后端 `COMPONENT_NAMES` 一字不差；成分的取值范围也正是这几项。"""
    from app.routers.blood import _COMPONENT, COMPONENT_NAMES

    line = top_const((STATIC / "pages-public.js").read_text(encoding="utf-8"), "BLOOD_COMPONENTS")
    if shutil.which("node"):
        done = subprocess.run(["node", "-e", f"{line}\nprocess.stdout.write(JSON.stringify(BLOOD_COMPONENTS));"],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        front = json.loads(done.stdout)
    else:
        front = dict(re.findall(r'(\w+): "([^"]*)"', line))
    assert front == COMPONENT_NAMES
    assert re.fullmatch(r"\^\((.+)\)\$", _COMPONENT).group(1).split("|") == list(COMPONENT_NAMES)


@needs_node
def test_页面队列印出申请人申请时间用血原因_患者印姓名(client, world):
    got = responses(client, world["director"])
    html = render("director", got)
    queue = html[html.index("<h3>用血申请队列</h3>"):]
    assert ("<th>ID</th><th>患者</th><th>机构</th><th>血型/成分</th><th>数量</th>"
            "<th>申请人</th><th>申请时间</th><th>用血原因</th><th>状态</th><th>操作</th>") in queue
    first_id, second_id = world["requests"]
    first = re.search(rf"<tr><td>{first_id}</td>[\s\S]*?</tr>", queue).group(0)
    second = re.search(rf"<tr><td>{second_id}</td>[\s\S]*?</tr>", queue).group(0)
    shown = _created_at(first_id)[:16].replace("T", " ")
    assert f"<td>P21469 赵一（#{world['patients'][0]}）</td>" in first   # 修前只印患者号
    assert "<td>A / 红细胞</td>" in first
    assert f"<td>张医生</td><td>{shown}</td>" in first
    assert "<td>术中失血 Hb 62g/L &lt;急&gt;</td>" in first   # 用血原因转义后照印
    assert f"<td>P21469 钱&lt;二&gt;（#{world['patients'][1]}）</td>" in second
    assert "<td>p21469_doc2</td>" in second and "<td>—</td>" in second   # 没写原因印「—」
    assert 'data-brev="' in first   # 待审批的照旧给「批准 / 驳回」

    # 成分列印后端给的文案，不再查前端那份表：后端将来加了前端表里没有的成分，页面照样印得出中文
    for path in got:
        for row in got[path] if path.startswith("/api/blood/requests") else []:
            if row["id"] == second_id:
                row["component"], row["component_name"] = "cryo", "冷沉淀"
    assert "<td>B / 冷沉淀</td>" in render("director", got)
