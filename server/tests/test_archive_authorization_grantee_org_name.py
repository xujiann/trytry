"""调阅授权的被授权机构从下拉里选、清单印机构名称（P2-1727，第五十一批扫描 AO2-8）。

修前：授权页「被授权机构ID」是数字框，清单「被授权机构」列只印 `grantee_org_id`——敲错一位就把整份档案授权给了别家，清单上
录错的与录对的看不出区别；而任一有效授权等于全部档案可调阅（P1-162）。同形的「手填编号、页面不显示名字」P2-1696 / P2-1430
已改成下拉。修法：页面被授权机构改为下拉（取 `/api/organizations`，选项印机构名，首项为空、必选）；`AuthorizationOut` 末尾追加
`grantee_org_name`，清单印名称。授权的判定、谁能授、授给谁的规则一律不动（后端照旧只看机构存在）。
"""
import re
import shutil

import pytest
from inpatient_page import selects_in
from patients_page import run

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"]
        for name in ("P21727 甲卫生院", "P21727 <i>乙</i>卫生院")]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21727 患者", "id_card": "330106197404041727"}).json()["id"]
    grant = client.post(f"/api/patients/{patient}/authorizations", headers=admin,
                        json={"grantee_org_id": orgs[1], "scope": "all", "expire_date": "2099-12-31"})
    assert grant.status_code == 201, grant.text
    return {"orgs": orgs, "patient": patient, "grant": grant.json()["id"]}


def test_授权清单末尾带被授权机构名称(client, admin, world):
    rows = client.get(f"/api/patients/{world['patient']}/authorizations", headers=admin).json()
    assert list(rows[0]) == ["id", "grantee_org_id", "scope", "expire_date", "status", "effective", "status_name",
                             "grantee_org_name"]   # 只加在末尾，原有键与次序不动
    assert (rows[0]["grantee_org_id"], rows[0]["grantee_org_name"]) == (world["orgs"][1], "P21727 <i>乙</i>卫生院")


def _grant_form(html: str) -> str:
    return re.search(r'<form class="inline" id="auth-grant-form">([\s\S]*?)</form>', html).group(1)


@needs_node
def test_授权表单没有机构ID数字框_被授权机构从下拉里选(client, admin, world):
    html, _ = run(client, admin, "await renderPatients(); return pageHtml();")
    form = _grant_form(html)
    assert 'name="grantee_org_id" type="number"' not in form and "被授权机构ID" not in form   # 修前是数字框
    assert '<select name="grantee_org_id" required>' in form
    options = selects_in(form)["grantee_org_id"]
    assert options[0] == ("", "选择被授权机构")   # 必选：不动下拉不会落在第一家机构上
    listed = client.get("/api/organizations", headers=admin).json()
    assert [value for value, _ in options[1:]] == [str(o["id"]) for o in listed]   # 选项就是机构清单，一个不落
    assert (str(world["orgs"][1]), "P21727 &lt;i&gt;乙&lt;/i&gt;卫生院") in options   # 经 esc()


@needs_node
def test_选了机构按数送(client, admin, world):
    steps = """
      await renderPatients();
      await submitForm("#auth-grant-form", { patient_id: String(ARGS.params.patient), grantee_org_id: String(ARGS.params.org),
                                             scope: "all", expire_date: "2099-12-31" });
      return posts;
    """
    posts, _ = run(client, admin, steps, {"patient": world["patient"], "org": world["orgs"][0]})
    assert posts == [["POST", f"/api/patients/{world['patient']}/authorizations",
                      {"grantee_org_id": world["orgs"][0], "scope": "all", "expire_date": "2099-12-31"}]]


@needs_node
def test_授权清单印机构名称(client, admin, world):
    steps = """
      await renderPatients();
      await submitForm("#auth-list-form", { patient_id: String(ARGS.params.patient) });
      return htmlOf("#auth-table");
    """
    html, _ = run(client, admin, steps, {"patient": world["patient"]})
    row = html[html.index(f"<tr><td>{world['grant']}</td>"):]
    row = row[:row.index("</tr>")]
    assert f"<td>{world['grant']}</td><td>P21727 &lt;i&gt;乙&lt;/i&gt;卫生院</td>" in row, row   # 修前只印机构编号，经 esc()
