"""已有病种的纳入 / 排除规则在运行中枢页上改得了，保存走「改规则即升版本」（P2-173）。

`PATCH /api/spd/programs/{id}` 改规则即升一版并留快照；页面原先没有改规则的入口——编辑对话框叫人「用下方编辑器
新建版本」，下方那两个编辑器挂在「新建病种」表单上：同编码提交 409，换个编码就多出一个病种。端到端档驱动整条链路，
这里在单元档钉住入口的形状，免得哪次改页面又把它丢了。
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _source():
    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        return fh.read()


def test_病种行上有改规则入口_保存走PATCH送两类规则():
    source = _source()
    assert 'data-prog-rules="${p.id}"' in source
    handler = source[source.index("if (progRules) {"):source.index("if (progVersions) {")]
    assert re.search(r'postAction\(`/api/spd/programs/\$\{prog\.id\}`, \{\s*include_rules: includeEdit\.value\(\), '
                     r'exclude_rules: excludeEdit\.value\(\)', handler), "改规则得 PATCH 到这个病种、两类规则都送"
    assert '"#spd-program-msg", "PATCH")' in handler


def test_不再叫人去新建病种的编辑器里改规则():
    assert "规则请用下方编辑器新建版本" not in _source()
