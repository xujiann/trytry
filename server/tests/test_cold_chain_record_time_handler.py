"""冷链记录看得出是什么时候测的、记得下谁在什么时候处置的（P2-1503，第四十四批「疫苗批次、冷链与 AEFI」扫描 AH1-7）。

修前三处：冷链页的表格只有「机构 设备 温度 区间 状态 处置」，没有记录时刻；清单按 id（录入顺序）倒序——扫描实测补录的
10-03 20:00 读数排在 10-05 08:00 的超温记录之上；出参没有录入时刻，处置只写说明，谁在什么时候处置的只能去翻审计路径。
兄弟路径室内质控把测定时刻与录入时刻分开，失控处理记 `handled_by` / `handled_at`（P2-492 / P2-1472）。

修后：清单按记录时刻倒序、id 倒序作尾键；出参末尾只增 `created_at`（录入时刻）、`handled_by`、`handled_at`；处置在判
「未处置」的同一条条件 UPDATE 里写处置人（full_name 或 username）与处置时刻；页面加「记录时刻」列，处置格写处置人与时刻
（截到分钟、经 `esc()`）。表结构加两列（迁移 `1401c485031c`，不回填存量：存量处置人未知）。
"""
import re
import shutil
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import ColdChainRecord, utcnow
from conftest import login
from vaccine_page import run

B = "/api/vaccine-supply"


@pytest.fixture(scope="module")
def ctx(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21503 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, role, full_name in (("p21503_ph", "public_health", "P21503 <公卫>张三"),
                                      ("p21503_op", "operator", "")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": full_name})
        assert created.status_code == 201, created.text
    return {"org": org, "ph": login(client, "p21503_ph", "passw0rd1"), "op": login(client, "p21503_op", "passw0rd1")}


def _record(client, ctx, device, temperature, recorded_at):
    resp = client.post(f"{B}/cold-chain", headers=ctx["ph"], json={
        "org_id": ctx["org"], "device_name": device, "temperature": temperature, "recorded_at": recorded_at})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rows(client, ctx, device):
    return [r for r in client.get(f"{B}/cold-chain", headers=ctx["ph"]).json() if r["device_name"] == device]


def test_补录早于已有记录的读数_清单按记录时刻倒序_同一时刻按id倒序(client, ctx):
    hot = _record(client, ctx, "P21503 1号冰箱", 13, "2026-10-05 08:00")
    late = _record(client, ctx, "P21503 1号冰箱", 5, "2026-10-03 20:00")   # 补录的前天读数
    same = _record(client, ctx, "P21503 1号冰箱", 6, "2026-10-05 08:00")   # 与超温那条同一时刻
    rows = _rows(client, ctx, "P21503 1号冰箱")
    # 修前按 id 倒序：[same, late, hot]——补录的 10-03 读数排在 10-05 的超温记录之上
    assert [r["id"] for r in rows] == [same["id"], hot["id"], late["id"]]
    assert [r["recorded_at"] for r in rows] == ["2026-10-05 08:00", "2026-10-05 08:00", "2026-10-03 20:00"]


def test_出参末尾增录入时刻_处置人与处置时刻_未处置为空串与null(client, ctx):
    before = utcnow()
    created = _record(client, ctx, "P21503 2号冰箱", 12, "2026-10-04 09:00")
    assert list(created)[-4:] == ["created_at", "handled_by", "handled_at", "hint"]   # 只在末尾增，条件键 hint 照旧殿后
    assert before - timedelta(seconds=1) <= datetime.fromisoformat(created["created_at"]) <= utcnow()
    assert (created["handled_by"], created["handled_at"]) == ("", None)
    (row,) = _rows(client, ctx, "P21503 2号冰箱")
    assert list(row)[-3:] == ["created_at", "handled_by", "handled_at"]
    assert (row["created_at"], row["handled_by"], row["handled_at"]) == (created["created_at"], "", None)


def test_处置回执与清单带处置人与处置时刻(client, ctx):
    hot = _record(client, ctx, "P21503 3号冰箱", 11, "2026-10-04 10:00")
    before = utcnow()
    handled = client.post(f"{B}/cold-chain/{hot['id']}/handle", headers=ctx["ph"],
                          json={"handle_note": "已转移至备用冰箱"})
    assert handled.status_code == 200, handled.text
    got = handled.json()
    assert got["handled_by"] == "P21503 <公卫>张三"   # 修前出参没有这两键、库里也不记
    assert before - timedelta(seconds=1) <= datetime.fromisoformat(got["handled_at"]) <= utcnow()
    (row,) = _rows(client, ctx, "P21503 3号冰箱")
    assert (row["handled"], row["handle_note"], row["handled_by"], row["handled_at"]) == \
        (True, "已转移至备用冰箱", "P21503 <公卫>张三", got["handled_at"])
    # 没有姓名的账号记用户名（同质控失控处理）
    other = _record(client, ctx, "P21503 4号冰箱", 15, "2026-10-04 11:00")
    by_op = client.post(f"{B}/cold-chain/{other['id']}/handle", headers=ctx["op"], json={"handle_note": "已报修"})
    assert by_op.status_code == 200 and by_op.json()["handled_by"] == "p21503_op", by_op.text
    # 再处置照旧 409（P2-308），处置人与时刻不被改写
    again = client.post(f"{B}/cold-chain/{hot['id']}/handle", headers=ctx["op"], json={"handle_note": "改写"})
    assert again.status_code == 409
    (row,) = _rows(client, ctx, "P21503 3号冰箱")
    assert (row["handled_by"], row["handled_at"]) == ("P21503 <公卫>张三", got["handled_at"])


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面加记录时刻列_处置格写处置人与时刻(client, ctx):
    hot = _record(client, ctx, "P21503 5号冰箱", 14, "2026-10-04 12:00")
    handled = client.post(f"{B}/cold-chain/{hot['id']}/handle", headers=ctx["ph"],
                          json={"handle_note": "已转移<备用>冰箱"}).json()
    legacy = _record(client, ctx, "P21503 6号冰箱", 16, "2026-10-04 13:00")
    with SessionLocal() as db:   # 加列之前处置的存量：处置人与时刻都没记
        row = db.get(ColdChainRecord, legacy["id"])
        row.handled, row.handle_note = True, "存量处置说明"
        db.commit()
    html = run(client, ctx["ph"], """
await goto(renderVaccineSupply);
return document.querySelector("#page-body").innerHTML;
""")
    table = html[html.index("<table><thead><tr><th>记录时刻</th>"):]
    table = table[:table.index("</table>")]
    head = re.findall(r"<th>([^<]*)</th>", table)
    assert head == ["记录时刻", "机构", "设备", "温度", "区间", "状态", "处置"]   # 修前没有记录时刻
    rows = {re.findall(r"<td>(.*?)</td>", row, re.S)[2]: re.findall(r"<td>(.*?)</td>", row, re.S)
            for row in table.split("<tr>")[2:]}
    minute = handled["handled_at"][:16].replace("T", " ")
    assert rows["P21503 5号冰箱"][0] == "2026-10-04 12:00"
    assert rows["P21503 5号冰箱"][6] == ('已转移&lt;备用&gt;冰箱<div style="font-size:12px">'
                                       f"P21503 &lt;公卫&gt;张三 {minute} 处置</div>")
    assert rows["P21503 6号冰箱"][6] == "存量处置说明"   # 存量只写说明
