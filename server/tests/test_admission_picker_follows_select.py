"""下拉里换了人、没点「切换」，病程 / 体征 / 护理照旧写进上一位（P1-231，第二十九批「页面状态残留」扫描 E2-1）。

医生移动端查房（`m/doctor.js`）与管理端住院临床文书（`pages-mgmt.js`）都是「下拉 + 切换按钮」：写入对象只在点了按钮时
才改。下拉显示乙、没点切换，病程与体征按甲写，回执照样「病程已记录」；区块不写姓名，屏幕上看不出没切过去——
`core.js` 的 `pickedId` 注释里点名要防的「屏幕上写着甲，病程记录写进了乙」。同一页还有：查房区块取数不比对请求序号、
失败没人接（先发的甲那一批晚到画在乙名下）；体征录完只清数值不清测量时刻（换人后下一位带着上一位的时刻）；门急诊文书
的「就诊ID」框改了号没点「载入」，处置 / 护理记录进上一次就诊。

修法：下拉一改就切换；查房区块按序号只画最后一次、失败说清楚并清掉上一位的区块；测量时刻一并清空；门急诊文书框里的号
与已载入的不一致时拦下提交、说明先载入。端到端用例在 `tests/e2e/test_flows.py`（选乙、不点切换、写病程，落在乙）。
"""
import re
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
DOCTOR = strip_comments((STATIC / "m" / "doctor.js").read_text(encoding="utf-8"))
MGMT = strip_comments((STATIC / "pages-mgmt.js").read_text(encoding="utf-8"))


def _slice(src, start_marker, end_marker="\n}\n"):
    start = src.index(start_marker)
    return src[start:src.index(end_marker, start)]


def test_查房下拉一改就切换写入对象():
    m = re.search(r'\$\("#round-adm"\)\.addEventListener\("change", async \(\) => \{(.*?)\n\}\);', DOCTOR, re.S)
    assert m, "查房下拉没有 change 监听：下拉显示乙、写入对象仍是甲"
    assert 'roundAdmissionId = Number($("#round-adm").value);' in m.group(1)
    assert "refreshRoundDetail()" in m.group(1)


def test_查房区块只画最后一次切换的那一位_失败说清楚():
    body = _slice(DOCTOR, "async function refreshRoundDetail()")
    assert "const seq = ++roundSeq;" in body
    assert body.count("if (seq !== roundSeq) return;") == 2, body   # 成功、失败两条路都比对
    assert "catch (err)" in body
    catch = body[body.index("catch (err)"):]
    assert '$("#round-notes").innerHTML = "";' in catch and '$("#round-vitals").innerHTML = "";' in catch
    # 取数用的是发请求那一刻的住院号，不是读回来时的全局变量
    assert "${admissionId}" in body and "${roundAdmissionId}" not in body


def test_体征录完连测量时刻一起清():
    body = DOCTOR[DOCTOR.index('$("#round-vital").addEventListener("submit"'):]
    body = body[:body.index("\n});")]
    cleared = re.search(r'\[("#rv-at",[^\]]*)\]\s*\.forEach\(\(s\) => \{ \$\(s\)\.value = ""; \}\)', body)
    assert cleared, "体征录完没清测量时刻：换人后下一位带着上一位的时刻"


def test_住院文书下拉一改就切换():
    assert '$("#doc-pick select").onchange = (e) => pickAdmission(e.target.value);' in MGMT
    body = MGMT[MGMT.index("const pickAdmission = "):]
    assert body.index("const pickAdmission = ") < body.index('$("#doc-pick").onsubmit')   # 按钮与下拉同一个动作
    assert 'localStorage.setItem("medplat_doc_adm", admissionId); route();' in body[:200]


def test_门急诊文书改了就诊号没载入就拦下():
    body = MGMT[MGMT.index("const encounterUnloaded = "):]
    helper = body[:body.index("};") + 2]
    assert "Number(typed || 0) === encounterId" in helper, helper
    for form in ("#od-treat", "#od-nurse"):
        handler = body[body.index(f'$("{form}").onsubmit'):]
        handler = handler[:handler.index("postAction(")]   # 拦截要在发请求之前
        assert 'if (encounterUnloaded("#od-msg")) return;' in handler, (form, handler)
    consent = body[body.index('$("#od-consent").onsubmit'):]
    consent = consent[:consent.index("postAction(")]
    link = consent[consent.index("link_encounter.checked"):]
    assert link.index('if (encounterUnloaded("#od-cmsg")) return;') < link.index("body.related_id = Number(encounterId);")
