"""门诊结构化病历表单填好就诊号、对上已有病历时，把六项原文带进表单（P2-1792，第五十三批「请求模型与 ORM 列的约束错位」扫描
AQ3-1，页面这一半）。

表单写着「同一就诊仅一份病历，再次提交为修正并复评」，可 `pages-clinical.js::renderQuality` 填好就诊号只回显这是谁的就诊
（P2-1631），不把已有病历带进表单；提交时六项一律送 `f.get(key) || ""`。医生按质控意见只补一项再提交，其余五项照页面送
空串，后端修正分支整行覆盖——扫描实测五项被清空、质控从乙级当场降成 43 分丙级，既往史里的「青霉素过敏」随之消失，回执照样
「病历已修正」，没有修订表、原文查不回。打印模板同一形状已按 P2-993 修成表单带出现值。

修法（只改页面，后端修正分支不动——改成「不传即不改」是接口语义变更，另行裁定）：就诊号对上已有病历时，按病历清单的
就诊号筛选取回这一份，六项原文带进表单（同回显：号又改了的旧回包不写）；对不上（新病历）时表单照旧为空——框里是上一个
号带进来的原文就清掉，免得挂到这一次就诊名下（取不到已有病历时同样清掉，并在消息行说明），自己先敲的字不动。

页面原样拿到 node 里跑（DOM 与请求管道取自 `inpatient_page.py`，请求转给真接口），表单垫上按名字取的控件。
"""
import json
import shutil
import subprocess

import pytest

from conftest import login
from inpatient_page import PRELUDE, _detail, _read, function_source

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 门诊病历表单按名字取控件（`form.encounter_id`、`form.chief_complaint`……）：先垫好这张表单，页面挂的监听落在这几个控件上。
#: `typeEncounter` 照浏览器填就诊号、触发 change；`snapshot` 照浏览器的 FormData 取当下各框的值交表。
#: 页面的 `api` 外面包一层「扣住回包」：请求照发、回包照收（管道按先后配对），地址以 `hold` 登记的片段结尾的那一次先不交给
#: 页面，`gates[i].open()` 才放行；`flush` 等到连着 5 拍没有在途请求（被扣住的那次回包已经收到，不算在途）
FORM = r"""
const gates = [];
let outstanding = 0;
const realApi = api;
api = (path, opts) => {
  outstanding += 1;
  const reply = realApi(path, opts);
  reply.then(() => { outstanding -= 1; }, () => { outstanding -= 1; });
  const gate = gates.find((g) => !g.taken && path.endsWith(g.tail));
  if (!gate) return reply;
  gate.taken = true;
  return gate.opened.then(() => reply);
};
function hold(tail) {
  const gate = { tail, taken: false };
  gate.opened = new Promise((resolve) => { gate.open = resolve; });
  gates.push(gate);
}
const flush = async () => {
  for (let quiet = 0; quiet < 5;) { await new Promise((r) => setTimeout(r, 5)); quiet = outstanding ? 0 : quiet + 1; }
};
const MR_KEYS = ["encounter_id", ...MR_FIELDS.map(([key]) => key)];
const mrForm = elements["#mr-form"] = { dataset: {}, innerHTML: "", textContent: "", className: "",
  classList: { add() {}, remove() {} }, ...Object.fromEntries(MR_KEYS.map((key) => [key, { value: "" }])) };
const typeEncounter = (id) => {
  mrForm.encounter_id.value = String(id);
  return mrForm.encounter_id.onchange({ target: mrForm.encounter_id });
};
const boxes = () => Object.fromEntries(MR_FIELDS.map(([key]) => [key, mrForm[key].value]));
const snapshot = () => ({ ...Object.fromEntries(MR_KEYS.map((key) => [key, mrForm[key].value])), querySelectorAll: () => [] });
const P = ARGS.params;
"""

FIELDS = ["chief_complaint", "present_illness", "past_history", "physical_exam", "diagnosis_basis", "treatment_plan"]
FULL = {
    "chief_complaint": "反复头晕3年，加重2天",
    "present_illness": "患者3年前无明显诱因出现头晕，多次测血压最高170/100mmHg，未规律服用降压药物；2天前头晕加重，"
                       "伴枕部胀痛，无恶心呕吐，无肢体活动障碍。",
    "past_history": "否认糖尿病、冠心病史；青霉素过敏",
    "physical_exam": "体温36.5℃，脉搏78次/分，呼吸18次/分，血压168/102mmHg，心肺听诊未见异常",
    "diagnosis_basis": "多次非同日测量血压≥140/90mmHg，伴头晕、枕部胀痛症状，已排除继发性高血压",
    "treatment_plan": "氨氯地平 5mg qd，低盐饮食，2周后复诊",
}


def _const(source: str, head: str, end: str) -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


def _script(steps: str) -> str:
    core, clinical = _read("core.js"), _read("pages-clinical.js")
    return (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + function_source(core, "function setMsg(") + function_source(core, "function encounterWho(")
        + function_source(clinical, "function formJson(") + function_source(clinical, "async function postAction(")
        + _const(clinical, "const MR_FIELDS = [", "];\n") + _const(clinical, "const MR_GRADE_COLOR", "\n")
        + function_source(clinical, "async function renderQuality(")
        + FORM
        + f"\n(async () => {{\nawait renderQuality();\n{steps}\n}})().then((r) => {{ process.stdout.write("
        + "JSON.stringify({ result: r }) + '\\n'); rl.close(); }, (e) => { console.error(e); process.exit(1); });\n"
    )


def _run(client, headers, steps: str, params: dict, failing: dict | None = None) -> dict:
    """在 node 里加载质量安全页跑 `steps`（页面已画好）；页面的请求以 `headers` 的身份转给真接口，写请求照发。
    `failing` 里的路径不发，直接回 `(状态码, 原因)`（取数失败）。"""
    proc = subprocess.Popen(["node", "-e", _script(steps), json.dumps({"params": params}, ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            line = proc.stdout.readline()
            if not line:
                proc.wait(timeout=30)
                raise AssertionError(f"node 没给出结果就退出了：{proc.stderr.read()}")
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            if message["path"] in (failing or {}):
                status, detail = failing[message["path"]]
                proc.stdin.write(json.dumps({"error": detail, "status": status}, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                continue
            resp = client.request(message["method"], message["path"], json=message["body"], headers=headers)
            total = resp.headers.get("X-Total-Count")
            reply = ({"data": resp.json(), "total": None if total is None else int(total)} if resp.status_code < 400
                     else {"error": _detail(resp), "status": resp.status_code})
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        raise AssertionError("请求停不下来")
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21792 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21792_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "P21792 医生"})
    assert created.status_code == 201, created.text
    doctor = login(client, "p21792_doc", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21792 张三", "id_card": "330106197001011792"}).json()["id"]

    def encounter():
        resp = client.post("/api/encounters", headers=doctor, json={"patient_id": patient, "org_id": org})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {"doctor": doctor, "encounter": encounter}


def _with_record(client, world) -> tuple[int, dict]:
    enc = world["encounter"]()
    resp = client.post("/api/quality/records", headers=world["doctor"], json={"encounter_id": enc, **FULL})
    assert resp.status_code == 201 and resp.json()["created"], resp.text
    return enc, resp.json()


def test_对上已有病历_六项原文带进表单_只改一项提交后其余五项与质控分不变(client, world):
    enc, first = _with_record(client, world)
    plan = "氨氯地平 5mg qd 加 缬沙坦 80mg qd，1周后复诊"
    out = _run(client, world["doctor"], """
await typeEncounter(P.enc);
const loaded = boxes();
const msg = msgOf("#mr-msg");
mrForm.treatment_plan.value = P.plan;   // 医生按质控意见只改治疗方案
await mrForm.onsubmit({ preventDefault() {}, target: snapshot() });
return { loaded, msg, after: msgOf("#mr-msg"), posts };
""", {"enc": enc, "plan": plan})
    assert out["loaded"] == FULL, out["loaded"]   # 修前六个框都是空的
    assert f"记录 #{first['record']['id']}" in out["msg"], out["msg"]
    assert out["posts"] == [["POST", "/api/quality/records", {"encounter_id": enc, **FULL, "treatment_plan": plan}]]
    assert out["after"] == f"病历已修正（记录 #{first['record']['id']}）"
    (row,) = client.get(f"/api/quality/records?encounter_id={enc}", headers=world["doctor"]).json()
    assert {k: row[k] for k in FIELDS} == {**FULL, "treatment_plan": plan}   # 修前其余五项被清空
    assert (row["qc_score"], row["qc_grade"]) == (first["qc"]["score"], first["qc"]["grade"])   # 修前当场降成丙级


def test_新就诊号表单为空_上一个号带进来的原文清掉_自己先敲的字不动(client, world):
    enc, _ = _with_record(client, world)
    fresh, fresh2 = world["encounter"](), world["encounter"]()
    out = _run(client, world["doctor"], """
await typeEncounter(P.enc);
const loaded = boxes();
await typeEncounter(P.fresh);   // 号敲错了，改成一次还没写病历的就诊
const cleared = { boxes: boxes(), msg: msgOf("#mr-msg") };
mrForm.chief_complaint.value = "咳嗽发热3天";   // 新病历：自己敲的
await typeEncounter(P.fresh2);
return { loaded, cleared, typed: boxes() };
""", {"enc": enc, "fresh": fresh, "fresh2": fresh2})
    assert out["loaded"] == FULL, out["loaded"]   # 修前不带出原文
    assert out["cleared"] == {"boxes": dict.fromkeys(FIELDS, ""), "msg": ""}, out["cleared"]   # 不把上一位的原文挂到这次就诊
    assert out["typed"] == {**dict.fromkeys(FIELDS, ""), "chief_complaint": "咳嗽发热3天"}   # 自己敲的不清


def test_号又改了_旧回包不往框里写(client, world):
    enc, _ = _with_record(client, world)
    fresh = world["encounter"]()
    out = _run(client, world["doctor"], """
hold(`records?encounter_id=${P.enc}`);
const late = typeEncounter(P.enc);   // 先填了有病历的号，病历的回包还没到就改成另一次还没写病历的就诊
await flush();
await typeEncounter(P.fresh);
gates[0].open();
await late;
const stale = { boxes: boxes(), msg: msgOf("#mr-msg") };
await typeEncounter(P.enc);
return { stale, again: boxes() };
""", {"enc": enc, "fresh": fresh})
    assert out["stale"] == {"boxes": dict.fromkeys(FIELDS, ""), "msg": ""}, out["stale"]   # 不把上一个号的原文填进来
    assert out["again"] == FULL, out["again"]   # 改回来照样带出（修前不带出）


def test_已有病历取不到_说出来_上一个号带进来的原文照样清掉(client, world):
    enc, _ = _with_record(client, world)
    other = world["encounter"]()
    out = _run(client, world["doctor"], """
await typeEncounter(P.enc);
const loaded = boxes();
await typeEncounter(P.other);
return { loaded, boxes: boxes(), msg: [msgOf("#mr-msg"), elements["#mr-msg"].className] };
""", {"enc": enc, "other": other}, failing={f"/api/quality/records?encounter_id={other}": (503, "P21792 服务暂不可用")})
    assert out["loaded"] == FULL, out["loaded"]   # 修前不带出
    assert out["boxes"] == dict.fromkeys(FIELDS, ""), out["boxes"]   # 上一位的原文不留在这次就诊的框里
    assert out["msg"] == [f"就诊 #{other} 的已有病历取不到：P21792 服务暂不可用", "msg err"], out["msg"]
