"""住院护理记录的护理级别：页面不动下拉就送「特级护理」，接口与表列的缺省却是二级护理（P2-988，第二十八批「缺省值」扫描
F4-3）。

住院那张表单的下拉按 `NURSING_LEVELS` 的键序生成、没有预选，首项是 special；formJson 每次都送下拉的当前值，护士不动下拉，
二级、三级护理的患者在护理记录（病历文书）上一律成了「特级护理」。接口不带这个字段时落的又是二级（`NursingIn`、表列缺省）。
门急诊护理那张表单特意把 level3 排在首位，与它自己的接口缺省一致。

修法：住院表单预选与 `NursingIn` 缺省同一个级别（`INPATIENT_NURSING_DEFAULT`）。改成预选本次住院上一条记录的级别另行定口径。
"""
import re
from pathlib import Path

from app.routers.clinical_docs import NursingIn

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_住院护理表单预选的就是接口缺省():
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    default = re.search(r'const INPATIENT_NURSING_DEFAULT = "(\w+)";', public).group(1)
    assert default == NursingIn.model_fields["nursing_level"].default == "level2"
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    form = mgmt[mgmt.index('<form class="inline" id="nursing-form">'):]
    form = form[:form.index("</form>")]
    select = form[form.index('<select name="nursing_level">'):form.index("</select>")]
    assert 'k === INPATIENT_NURSING_DEFAULT ? " selected" : ""' in select   # 修前没有预选，首项 special
