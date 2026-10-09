"""居民端满意度页默认五星：只写了意见没点星就提交，记成 5 分，带评语的投诉进不了差评清单（P2-1777，第五十二批扫描 AP1-2）。

修前：`index.html` 的 `#sv-stars` 写死 `data-score="5"`，`m.js` 加载时再 `paintStars(5)`，提交取的就是这个分——页面一打开五颗星
全亮，写「家医从不上门」直接提交记 5 分：不进满意度页的差评清单（≤2 分，`surveys.NEGATIVE_SCORE`），还拉高均分、压低差评率。
评价类型下拉默认「家医签约服务」，没选的也记在签约上；提交后星数不复位，下一条沿用上一条的分。后端 `MySurveyIn.score` 本是
必填、没有缺省值，P1-136 定过「没答的不能当成答了」（单选默认选中第一项已修）。

修法：星级初始为空、类型下拉加空项「请选择评价类型」并必选；没选类型、没点星不发请求，在 `#survey-msg` 说出来；提交成功后
星级与类型复位。后端不动。

页面那条把 `m.js` 满意度一节原文（加载时就跑的那几句连同提交监听）放进 node 跑，星级与类型的初始状态照 `index.html` 写的取，
`authApi` 记下请求。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HTML = (STATIC / "m" / "index.html").read_text(encoding="utf-8")
M_JS = (STATIC / "m" / "m.js").read_text(encoding="utf-8")


def _tag(tag_id: str) -> str:
    return re.search(rf'<[a-z]+ id="{tag_id}"[^>]*>', HTML).group(0)


def test_页面不预置分_不预选类型():
    assert "data-score" not in _tag("sv-stars")   # 修前 data-score="5"
    select = HTML[HTML.index('<select id="sv-type"'):]
    select = select[:select.index("</select>")]
    assert re.findall(r'<option value="([^"]*)"', select)[0] == ""   # 修前首项「家医签约服务」
    assert "required" in _tag("sv-type")
    assert not re.search(r"paintStars\(\s*[1-5]\s*\)", strip_comments(M_JS))   # 修前加载时 paintStars(5)


PRELUDE = r"""
const OUT = { requests: [] };
function classList() {
  const s = new Set();
  return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
    toggle: (c, on) => ((on ?? !s.has(c)) ? s.add(c) : s.delete(c)) };
}
function makeEl(extra = {}) {
  const el = { innerHTML: "", textContent: "", value: "", className: "", dataset: {}, listeners: {}, classList: classList(),
    querySelectorAll: () => [], ...extra };
  el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
  return el;
}
const INIT = JSON.parse(process.argv[1]);   // index.html 写的初始状态
const spans = [1, 2, 3, 4, 5].map((v) => makeEl({ dataset: { v: String(v) } }));
const registry = {
  "#sv-stars": makeEl({ dataset: { ...INIT.stars }, querySelectorAll: () => spans }),
  "#sv-type": makeEl({ value: INIT.type }),
};
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl()) };
globalThis.history = { replaceState() {} };
async function authApi(path, options = {}) {
  OUT.requests.push({ path, body: JSON.parse(options.body) });
  return { id: 1, submitted: true };
}
function isAuthed() { return true; }
function switchTab() {}
"""

STEPS = r"""
const stars = $("#sv-stars"), type = $("#sv-type"), msg = $("#survey-msg"), comment = $("#sv-comment");
const submit = () => $("#survey-form").listeners.submit({ preventDefault() {} });
const lit = () => spans.filter((s) => s.classList.contains("on")).length;
const out = { loaded: { score: stars.dataset.score ?? null, lit: lit(), type: type.value } };
type.value = "contract"; comment.value = "家医从不上门";        // ① 选了类型、写了意见、没点星
await submit();
out.noStar = { requests: OUT.requests.length, msg: msg.textContent, cls: msg.className };
type.value = ""; stars.listeners.click({ target: spans[1] });   // ② 点了两星、类型没选
await submit();
out.noType = { requests: OUT.requests.length, msg: msg.textContent, cls: msg.className };
type.value = "contract";                                         // ③ 都选了：照实交两星
await submit();
out.sent = { requests: OUT.requests.slice(), msg: msg.textContent, cls: msg.className, type: type.value,
  score: stars.dataset.score, lit: lit(), comment: comment.value };
type.value = "encounter";                                        // ④ 紧接着再交一条：不沿用上一条的分
await submit();
out.again = { requests: OUT.requests.length, msg: msg.textContent };
return out;
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_没点星_没选类型不发请求_交了之后复位():
    section = M_JS[M_JS.index("/* ---------------- 满意度评价 ---------------- */"):
                   M_JS.index("/* ---------------- 价格公示")]
    start = M_JS.index("function setMsg(")
    set_msg = M_JS[start:M_JS.index("\n}\n", start) + 3]
    stars = re.search(r'data-score="(\d)"', _tag("sv-stars"))
    select = HTML[HTML.index('<select id="sv-type"'):]
    init = {"stars": {"score": stars.group(1)} if stars else {},
            "type": re.search(r'<option value="([^"]*)"', select).group(1)}
    script = (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + set_msg + section
              + f"\n(async () => {{\n{STEPS}\n}})().then("
              + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")
    done = subprocess.run(["node", "-e", script, json.dumps(init)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["loaded"] == {"score": None, "lit": 0, "type": ""}, out["loaded"]   # 修前 5 分、五星全亮、类型是签约
    assert out["noStar"] == {"requests": 0, "msg": "请先点星打分（1～5 星）再提交", "cls": "msg err"}, out   # 修前照 5 分交了
    assert out["noType"] == {"requests": 0, "msg": "请选择评价类型", "cls": "msg err"}, out
    sent = out["sent"]
    assert sent["requests"] == [{"path": "/api/portal/me/surveys",
                                 "body": {"target_type": "contract", "score": 2, "comment": "家医从不上门"}}]
    assert (sent["msg"], sent["cls"]) == ("评价已提交，感谢您的反馈！", "msg ok")
    assert (sent["type"], sent["score"] in (0, "0"), sent["lit"], sent["comment"]) == ("", True, 0, ""), sent   # 复位
    assert out["again"] == {"requests": 1, "msg": "请先点星打分（1～5 星）再提交"}, out   # 修前沿用上一条的分照交
