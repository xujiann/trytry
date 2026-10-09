"""建床位从病区下拉里选（显示「机构名 · 病区名」），不再手敲病区 ID；建病区的回执写上新病区的名字与编号
（P2-1696，第五十批扫描 AN4-4）。

修前：住院页「建床位」表单是「病区ID」数字框，而病区 ID 在任何页面上都看不到（页面取了病区清单，只拿来把床位表的病区 ID 换成
病区名）；`create_bed` 只看病区存在（管理员是全域角色）。实测管理员把甲院的床位填成乙院病区的 ID：201，这张床计入乙院床位效率、
乙院能把病人收进去，而床位删不掉、改不了（P2-1357）。同页转床早由 P2-38 改成从本机构空床里选。

修法：用页面已取到的病区清单做下拉（「机构名 · 病区名」，值为病区 id，一律 esc()；首项是空的「选择病区」，必选）；建病区成功后
回执写上新病区的名字与编号。后端不动。页面原样拿到 node 里跑、请求转给真接口（夹具见 `tests/inpatient_page.py`）。
"""
import re
import shutil

import pytest
from inpatient_page import run, selects_in

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"] for name in ("P21696 甲卫生院", "P21696 乙卫生院")]
    wards = [client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": name}).json()["id"]
             for org, name in zip(orgs, ("P21696 内科病区", "P21696 <i>外科</i>病区"))]
    return {"orgs": orgs, "wards": wards}


def _bed_form(html: str) -> str:
    return re.search(r'<form class="inline" id="bed-form">([\s\S]*?)</form>', html).group(1)


def test_建床位表单没有病区ID数字框_病区从下拉里选_显示机构与病区名(client, admin, world):
    html = run(client, admin, "await renderInpatient(); return pageHtml();")
    form = _bed_form(html)
    assert 'name="ward_id" type="number"' not in form and "病区ID" not in form   # 修前是「病区ID」数字框
    options = selects_in(form)["ward_id"]
    assert '<select name="ward_id" required>' in form
    assert options[0] == ("", "选择病区（机构 · 病区）")   # 必选：不动下拉不会落在第一个病区上
    assert (str(world["wards"][0]), "P21696 甲卫生院 · P21696 内科病区") in options
    assert (str(world["wards"][1]), "P21696 乙卫生院 · P21696 &lt;i&gt;外科&lt;/i&gt;病区") in options   # 经 esc()
    listed = client.get("/api/inpatient/wards", headers=admin).json()
    assert [value for value, _ in options[1:]] == [str(w["id"]) for w in listed]   # 选项就是病区清单，一个不落


def test_选了病区按数送(client, admin, world):
    steps = """
      await renderInpatient();
      await submitForm("#bed-form", { ward_id: String(ARGS.params.ward), bed_no: "P21696-01" });
      await until(() => ROUTED > 0);   // 建床位照旧走 postAction（同步处理），等它的请求往返落定
      return posts;
    """
    posts = run(client, admin, steps, {"ward": world["wards"][0]})
    assert posts == [["POST", "/api/inpatient/beds", {"ward_id": world["wards"][0], "bed_no": "P21696-01"}]]


def test_建病区回执写上新病区的名字与编号(client, admin, world):
    steps = """
      await renderInpatient();
      await submitForm("#ward-form", { org_id: String(ARGS.params.org), name: "P21696 儿科病区" });
      await until(() => ROUTED > 0);   // 修前走 postAction（同步处理）：等它的请求往返落定
      return { msg: msgOf("#inp-msg"), routed: ROUTED };
    """
    got = run(client, admin, steps, {"org": world["orgs"][0]}, send_writes=True)
    (ward,) = [w for w in client.get("/api/inpatient/wards", headers=admin).json() if w["name"] == "P21696 儿科病区"]
    assert got["routed"] == 1
    # 修前走 postAction：成功即整页重画，回执一个字没有，新病区的编号在页面上哪里都看不到
    assert "P21696 儿科病区" in got["msg"] and f"编号 {ward['id']}" in got["msg"], got["msg"]
