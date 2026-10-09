"""建病区的机构从下拉里选（显示机构名），不再手敲机构 ID（P2-1743，第五十批 AN4 修复回报附带）。

修前：住院页「建病区」表单是「机构ID」数字框；`create_ward` 只看机构存在（管理员是全域角色）——敲错一位就把病区建进别家机构，
计入人家的床位效率、人家能把病人收进去，而病区改不了名、撤不掉（P2-1357）。同一张表单里的建床位早由 P2-1696 改成从病区下拉里选，
页面也已取到机构清单。

修法：用页面已取到的机构清单做下拉（值为机构 id、显示机构名，一律 esc()；首项是空的「选择机构」，必选）。后端不动。
页面原样拿到 node 里跑、请求转给真接口（夹具见 `tests/inpatient_page.py`）。
"""
import re
import shutil

import pytest
from inpatient_page import run, selects_in

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"]
        for name in ("P21743 甲卫生院", "P21743 <b>乙</b>卫生院")]
    return {"orgs": orgs}


def _ward_form(html: str) -> str:
    return re.search(r'<form class="inline" id="ward-form">([\s\S]*?)</form>', html).group(1)


def test_建病区表单没有机构ID数字框_机构从下拉里选(client, admin, world):
    html = run(client, admin, "await renderInpatient(); return pageHtml();")
    form = _ward_form(html)
    assert 'name="org_id" type="number"' not in form and "机构ID" not in form   # 修前是「机构ID」数字框
    assert '<select name="org_id" required>' in form
    options = selects_in(form)["org_id"]
    assert options[0] == ("", "选择机构")   # 必选：不动下拉不会落在第一家机构上
    assert (str(world["orgs"][0]), "P21743 甲卫生院") in options
    assert (str(world["orgs"][1]), "P21743 &lt;b&gt;乙&lt;/b&gt;卫生院") in options   # 经 esc()
    listed = client.get("/api/organizations", headers=admin).json()
    assert [value for value, _ in options[1:]] == [str(o["id"]) for o in listed]   # 选项就是机构清单，一个不落


def test_选了机构按数送_病区建在所选机构(client, admin, world):
    steps = """
      await renderInpatient();
      await submitForm("#ward-form", { org_id: String(ARGS.params.org), name: "P21743 内科病区" });
      await until(() => ROUTED > 0);
      return { posts, msg: msgOf("#inp-msg") };
    """
    got = run(client, admin, steps, {"org": world["orgs"][1]}, send_writes=True)
    assert got["posts"] == [["POST", "/api/inpatient/wards", {"org_id": world["orgs"][1], "name": "P21743 内科病区"}]]
    (ward,) = [w for w in client.get("/api/inpatient/wards", headers=admin).json() if w["name"] == "P21743 内科病区"]
    assert ward["org_id"] == world["orgs"][1]
    assert f"编号 {ward['id']}" in got["msg"]   # P2-1696 的回执照旧
