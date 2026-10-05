"""慢病建档的病种、随访中心补建的类别、专病入组的机构、专病记录节点四处下拉不再悄悄落在第一项（P2-1542，第四十五批扫描
AI2-2）。

修前四处下拉都不给空项，浏览器落在第一项（扫描实测，修前代码）：
- 慢病建档的病种缺省「高血压」：给糖尿病患者建档没动下拉，201 建成高血压档案；慢病档案只有建档、记随访两个写接口，
  改病种、删档都 404，再建对的糖尿病档案后名下两份，假的高血压档案进在管名单、慢病在管人数、绩效分母，90 天后进超期名单；
- 随访中心「补建任务」的类别缺省「慢病随访」：补建的出院 / 术后随访记成慢病随访，表单没有标题栏，标题也落成「慢病随访」；
- 专病入组的机构缺省机构表第一家：admin 不动下拉就把病例记到那一家，之后本机构医生记节点、出组都 403；非全域医生直接 403；
- 专病「记录节点」的节点缺省首节点（`value: nodes[0].key`）：不动下拉就把首节点记成已完成，节点记录没有删除入口。

修法照 P2-1443（消毒供应申领下拉）：三张表单的下拉首项为值为空的占位、`required`，没选就不发请求、提示写在表单的消息行；
记录节点的框给 `placeholder`（`spdModal` 的 select 给了 placeholder 即必选），没选框不关、提示写在框里。后端判定不动。
三页原样拿到 node 里跑、请求转给真接口（夹具见 `tests/chronic_followup_pages.py`），写请求只记下、不发。
"""
import shutil

import pytest

from chronic_followup_pages import run
from conftest import business_today

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    """县医院、东镇两家机构，一位患者；一个三节点的专病目录，该患者在东镇入了组。"""
    orgs = {}
    for key, name, level, kind in (("county", "P21542 县人民医院", "county", "lead_hospital"),
                                   ("east", "P21542 东镇卫生院", "township", "township")):
        resp = client.post("/api/organizations", headers=admin, json={"name": name, "org_type": kind, "level": level})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21542 患者", "id_card": "330106196001011542"}).json()["id"]
    program = client.post("/api/disease-programs", headers=admin, json={
        "code": "P21542_STK", "name": "P21542 脑卒中", "path_nodes": [
            {"key": "assess", "name": "首次评估"}, {"key": "treat", "name": "康复治疗"},
            {"key": "review", "name": "疗效复评"}]})
    assert program.status_code == 201, program.text
    other = client.post("/api/patients", headers=admin, json={
        "name": "P21542 入组患者", "id_card": "330106196001021542"}).json()["id"]
    enrollment = client.post(f"/api/disease-programs/{program.json()['id']}/enrollments", headers=admin, json={
        "patient_id": other, "org_id": orgs["east"]})
    assert enrollment.status_code == 201, enrollment.text
    return {"orgs": orgs, "patient": patient, "program": program.json()["id"], "enrollment": enrollment.json()["id"]}


#: 表单两次提交：先照浏览器不动下拉的值交（`untouched`），再选一项交。`ARGS.params`：render（页面函数名）、form（表单 id）、
#: select（下拉名）、msg（消息行）、fields（其余字段）、pick（第二次选的值）
FORM_STEPS = r"""
const P = ARGS.params;
await globalThis[P.render]();
const select = formSelects(pageHtml(), P.form)[P.select];
await submitForm(`#${P.form}`, { ...P.fields, [P.select]: untouched(select) });
const untouchedRun = { posts: posts.splice(0), msg: msgOf(P.msg) };
await submitForm(`#${P.form}`, { ...P.fields, [P.select]: String(P.pick) });
return { select, untouched: untouchedRun, picked: posts };
"""


def _form(client, admin, storage=None, **params):
    return run(client, admin, "admin", FORM_STEPS, params, storage=storage)


def _assert_blank_first(select, placeholder):
    assert select["required"] is True, select                                          # 修前没有 required
    assert select["options"][0] == {"value": "", "label": placeholder, "selected": False}, select   # 修前首项就是第一项
    assert not any(o["selected"] for o in select["options"]), select                    # 缺省就落在这个空项上


def test_慢病建档的病种首项为空必选_不选不发请求(client, admin, world):
    got = _form(client, admin, render="renderChronic", form="chronic-form", select="disease", msg="#chronic-msg",
                fields={"patient_id": str(world["patient"]), "managed_by_org_id": str(world["orgs"]["east"])},
                pick="diabetes")
    _assert_blank_first(got["select"], "请选择病种")
    assert [o["value"] for o in got["select"]["options"][1:3]] == ["hypertension", "diabetes"]
    # 修前：不动下拉就送出 hypertension，201 建成高血压档案
    assert got["untouched"] == {"posts": [], "msg": "请选择病种"}
    assert got["picked"] == [["POST", "/api/chronic", {
        "patient_id": world["patient"], "disease": "diabetes", "managed_by_org_id": world["orgs"]["east"]}]]


def test_随访中心补建任务的类别首项为空必选_不选不发请求(client, admin, world):
    due = business_today().isoformat()
    got = _form(client, admin, render="renderFollowups", form="fu-form", select="category", msg="#fu-msg",
                fields={"patient_id": str(world["patient"]), "org_id": str(world["orgs"]["east"]), "due_date": due,
                        "assigned_to": ""},
                pick="discharge")
    _assert_blank_first(got["select"], "请选择随访类别")
    assert [o["label"] for o in got["select"]["options"][1:]] == ["慢病随访", "出院随访", "术后随访", "妇幼访视"]
    assert got["untouched"] == {"posts": [], "msg": "请选择随访类别"}   # 修前记成「慢病随访」
    assert got["picked"] == [["POST", "/api/followups", {
        "patient_id": world["patient"], "org_id": world["orgs"]["east"], "category": "discharge", "due_date": due}]]


def test_专病入组的机构首项为空必选_不选不发请求(client, admin, world):
    program, east = world["program"], world["orgs"]["east"]
    got = _form(client, admin, storage={"medplat_program": str(program)}, render="renderDiseasePrograms",
                form="dp-enroll", select="org_id", msg="#dp-msg", fields={"patient_id": str(world["patient"])},
                pick=east)
    _assert_blank_first(got["select"], "请选择入组机构")
    assert {o["label"] for o in got["select"]["options"][1:]} >= {"P21542 县人民医院", "P21542 东镇卫生院"}
    assert got["untouched"] == {"posts": [], "msg": "请选择入组机构"}   # 修前记到机构表第一家
    assert got["picked"] == [["POST", f"/api/disease-programs/{program}/enrollments",
                              {"patient_id": world["patient"], "org_id": east}]]


def test_专病记录节点的节点首项为空_不选框不关_框里提示(client, admin, world):
    enrollment = world["enrollment"]
    got = run(client, admin, "admin", r"""
await renderDiseasePrograms();
const pending = click({ dpnode: String(ARGS.params.enrollment) });
await tick();
const modal = lastModal();
const options = selectsIn(modal.html).node_key.options;
await submitModal(modal);
const untouchedRun = { posts: posts.splice(0), closed: modal.removed, msg: modalMsg(modal) };
await submitModal(modal, { node_key: "review" });
await pending;
return { options, untouched: untouchedRun, picked: { posts, closed: modal.removed } };
""", {"enrollment": enrollment}, storage={"medplat_program": str(world["program"])})
    assert got["options"] == [{"value": "", "label": "请选择节点", "selected": False},   # 修前缺省首节点「首次评估」
                              {"value": "assess", "label": "首次评估", "selected": False},
                              {"value": "treat", "label": "康复治疗", "selected": False},
                              {"value": "review", "label": "疗效复评", "selected": False}]
    assert got["untouched"] == {"posts": [], "closed": False, "msg": "请选择节点"}   # 修前把首节点记成已完成、框关了
    assert got["picked"] == {"posts": [["POST", f"/api/disease-programs/enrollments/{enrollment}/records", {
        "node_key": "review", "performed_at": "", "operator_name": "", "result": "", "note": ""}]], "closed": True}
