"""慢专病三处「先取数、取回来才开框」的入口：取数期间再点不再叠第二张框，框头写明是谁（P2-1793，第五十三批扫描 AQ2-2）。

修前：随访管理点「执行」先取这一条的前置资料、取回来才开 `spdModal`，取数在途时清单照样能点——先点甲行（点错行）、一个往返内
又点乙行，叠出两张 HTML 一模一样的框，框头只写问卷名（「执行随访 · 慢病随访问卷」）。回包按顺序到时上面那张是乙的，交完底下
还剩一张，再交就写进甲；甲的前置资料晚到时上面那张就是甲的，一次正常提交就把乙的随访结果写进甲的随访记录（扫描实测：
「王甲 记录#2 已完成 结果='乙：…'」「赵乙 记录#5 待随访」）。同形：筛查登记选了量表（先取量表）、成员端开展评估（先取量表，
结论回写档案风险，高危自动派干预与复诊）。

修法：入口记一个「开框中」，取数期间与框开着时再点不开第二张框（框关了才放下）；框头写「姓名 · 记录 #id · 计划日」，两处量表
写患者号（量表出参不带患者）。共享诊断开单的「可互认」框同形，见 `test_exam_recognition_modal_once.py`。页面原样拿到 node 里跑
（夹具 `tests/page_race.py`，可扣住个别回包），请求转给真接口。
"""
import shutil

import pytest

from page_race import run, spd_page_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21793 东镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = {}
    for name, card in (("P21793王甲", "330102195001011793"), ("P21793赵乙", "330102195202021793")):
        resp = client.post("/api/patients", headers=admin, json={"name": name, "id_card": card})
        assert resp.status_code == 201, resp.text
        patients[name] = resp.json()["id"]
    quest = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P21793_Q", "name": "P21793 慢病随访问卷",
        "items": [{"key": "bp", "title": "收缩压", "type": "number"}]})
    assert quest.status_code == 201, quest.text
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P21793_R", "name": "P21793 随访方案", "points": [0], "questionnaire_code": "P21793_Q"})
    assert rule.status_code == 201, rule.text
    records = {}
    for name, pid in patients.items():
        plan = client.post(f"{B}/followup-plans", headers=admin, json={
            "patient_id": pid, "rule_id": rule.json()["id"], "org_id": org})
        assert plan.status_code == 201, plan.text
        records[name] = plan.json()["items"][0]
    yes_no = [{"label": "是", "score": 3}, {"label": "否", "score": 0}]
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "P21793_S", "version": "v1", "name": "P21793 高血压自评", "category": "screen",
        "program_code": "hypertension", "items": [{"key": "salt", "title": "口味偏咸", "type": "single", "options": yes_no}],
        "scoring": {"ranges": [{"min": 0, "max": 2, "risk": "low"}, {"min": 3, "max": None, "risk": "high"}]}})
    assert scale.status_code == 201, scale.text
    assert client.post(f"{B}/scales/{scale.json()['id']}/publish", headers=admin).status_code == 200
    return {"patients": patients, "records": records, "scale": scale.json()}


def _record(client, admin, record_id):
    rows = client.get(f"{B}/followup-records", headers=admin, params={"limit": 200}).json()
    return next(r for r in rows if r["id"] == record_id)


def test_执行随访_连点两行只开一张框_框头写姓名记录号与计划日(client, admin, world):
    a, b = world["records"]["P21793王甲"], world["records"]["P21793赵乙"]
    out = run(client, admin, spd_page_js(), """
      await renderSpdFollowup(); await idle();
      const before = posts.length;
      // 先点甲行的「执行」（点错行），没等框出来立刻点乙行
      click({ fuExec: String(ARGS.params.a) });
      click({ fuExec: String(ARGS.params.b) });
      await idle();
      const open = openModals();
      const first = { count: open.length, titles: open.map(titleOf), intro: open.length ? introOf(open[0]) : "" };
      await cancelModal(open[0]);
      // 框关了才放下：再点乙行，开的是乙的框
      click({ fuExec: String(ARGS.params.b) });
      await idle();
      const again = openModals().map(titleOf);
      await cancelModal(openModals()[0]);
      return { first, again, writes: posts.length - before };
    """, params={"a": a["id"], "b": b["id"]})
    assert out["first"]["count"] == 1, out   # 修前两张一模一样的框
    assert out["first"]["titles"] == [f"执行随访 · P21793王甲 · 记录 #{a['id']} · 计划 {a['planned_at']}"], out
    assert out["first"]["intro"] == "问卷：P21793 慢病随访问卷", out   # 问卷名挪到框头下
    assert out["again"] == [f"执行随访 · P21793赵乙 · 记录 #{b['id']} · 计划 {b['planned_at']}"], out
    assert out["writes"] == 0


def test_执行随访_扣住甲的前置资料_交上去的仍是框头那一条(client, admin, world):
    a, b = world["records"]["P21793王甲"], world["records"]["P21793赵乙"]
    out = run(client, admin, spd_page_js(), """
      await renderSpdFollowup(); await idle();
      const ctx = `/api/spd/followup-records/${ARGS.params.a}/context`;
      hold(ctx);
      click({ fuExec: String(ARGS.params.a) });
      click({ fuExec: String(ARGS.params.b) });   // 甲的前置资料还没回来
      await idle();
      const whileHeld = openModals().length;
      const released = await release(ctx);
      const open = openModals();
      const titles = open.map(titleOf);
      await submitModal(open[open.length - 1], { result: "甲：血压 150/95，已调药" });
      return { whileHeld, released, titles, left: openModals().length,
               executes: posts.filter((p) => p[1].endsWith("/execute")).map((p) => p[1]) };
    """, params={"a": a["id"], "b": b["id"]})
    assert out["whileHeld"] == 0 and out["released"] == 1, out
    assert out["titles"] == [f"执行随访 · P21793王甲 · 记录 #{a['id']} · 计划 {a['planned_at']}"], out   # 修前两张同样的框
    assert out["left"] == 0, out   # 修前交完还剩一张，再交就写进另一位
    assert out["executes"] == [f"{B}/followup-records/{a['id']}/execute"], out
    done = _record(client, admin, a["id"])
    assert (done["status"], done["result"]) == ("done", "甲：血压 150/95，已调药"), done
    assert _record(client, admin, b["id"])["status"] == "planned"   # 乙的那条没被写


def test_筛查逐题作答_取量表期间再交不叠框_框头写患者号(client, admin, world):
    a, b = world["patients"]["P21793王甲"], world["patients"]["P21793赵乙"]
    out = run(client, admin, spd_page_js(), """
      await renderSpdPatients(); await idle();
      const scale = `/api/spd/scales/${ARGS.params.scale}`;
      hold(scale);
      const form = (pid) => ({ patient_id: String(pid), program_code: "hypertension", source: "opportunistic",
                               scale_code: "P21793_S" });
      submitForm("#spd-screen-form", form(ARGS.params.a));
      submitForm("#spd-screen-form", form(ARGS.params.b));   // 改了患者号又点一次「登记筛查」
      await idle();
      const released = await release(scale);
      const open = openModals();
      const titles = open.map(titleOf);
      await submitModal(open[open.length - 1], { q_salt: "是" });
      return { released, titles, left: openModals().length,
               screenings: posts.filter((p) => p[1] === "/api/spd/screenings").map((p) => p[2].patient_id) };
    """, params={"a": a, "b": b, "scale": world["scale"]["id"]})
    assert out["released"] == 1, out   # 第二次提交没再取量表
    assert out["titles"] == [f"P21793 高血压自评 · 患者 {a} · 逐题作答（没答的题留空）"], out   # 修前两张框、框头只有量表名
    assert out["left"] == 0 and out["screenings"] == [a], out
    rows = client.get(f"{B}/screenings", headers=admin, params={"patient_id": a}).json()
    assert [r["answers"] for r in rows] == [{"salt": "是"}], rows
    assert client.get(f"{B}/screenings", headers=admin, params={"patient_id": b}).json() == []


def test_评估逐题作答_取量表期间再交不叠框_框头写患者号(client, admin, world):
    a, b = world["patients"]["P21793王甲"], world["patients"]["P21793赵乙"]
    out = run(client, admin, spd_page_js(), """
      await renderSpdMember(); await idle();
      const scale = `/api/spd/scales/${ARGS.params.scale}`;
      hold(scale);
      submitForm("#spd-assess-form", { patient_id: String(ARGS.params.a), scale_id: String(ARGS.params.scale) });
      submitForm("#spd-assess-form", { patient_id: String(ARGS.params.b), scale_id: String(ARGS.params.scale) });
      await idle();
      const released = await release(scale);
      const open = openModals();
      const titles = open.map(titleOf);
      await submitModal(open[open.length - 1], { q_salt: "否" });
      return { released, titles, left: openModals().length, msg: textOf("#spd-assess-msg"),
               assessments: posts.filter((p) => p[1] === "/api/spd/assessments").map((p) => p[2].patient_id) };
    """, params={"a": a, "b": b, "scale": world["scale"]["id"]})
    assert out["released"] == 1, out
    assert out["titles"] == [f"P21793 高血压自评 · 患者 {a} · 逐题作答（没答的题留空）"], out   # 修前两张框、框头只有量表名
    assert out["left"] == 0 and out["assessments"] == [a], out
    assert out["msg"].startswith("评估完成：0 分"), out
    assert client.get(f"{B}/assessments", headers=admin, params={"patient_id": b}).json() == []
