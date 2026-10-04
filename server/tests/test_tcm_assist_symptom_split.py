"""智能辨证切词认顿号与空白（P2-1409，第四十一批扫描 AE4-6）。

修前页面按 `/[,，]/` 切词、后端拿整项去比：「乏力、气短、自汗」（顿号连写）、「乏力 气短 自汗」（空格连写）都被当成**一个**
症状送出去，推荐为空表——看起来像这组症状没有对应证型；「畏寒，肢冷、腰膝酸软」只认出畏寒，阳虚证命中 1 个而不是 3 个，
排名也跟着变。同仓的智能导诊台（core.js `renderAppointments`）切词用 `/[，,、\\s]+/`；后端 `texttypes.split_list`（P1-137）
也写明「逗号分隔」的清单要认顿号。

修后页面照抄导诊台那个切词正则（注释写明出处，以及为什么不抽到 shared.js）；后端 `assist_diagnosis` 再用 `split_list` 把
每一项拆开、去空白，再按空白拆，收进集合去重——直接调接口、把整串当一项送的对接方同样认得。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 气虚证三个症状逐项送的结果（修前修后都是它）
QI_DEFICIENCY = {"syndrome": "气虚证", "matched": ["乏力", "气短", "自汗"], "match_count": 3, "formula": "四君子汤",
                 "techniques": ["艾灸足三里", "穴位贴敷"]}


def _assist(client, admin, symptoms):
    resp = client.post("/api/tcm/assist-diagnosis", headers=admin, json={"symptoms": symptoms})
    assert resp.status_code == 200, resp.text
    return resp.json()["recommendations"]


def test_逐项送的照旧(client, admin):
    assert _assist(client, admin, ["乏力", "气短", "自汗"]) == [QI_DEFICIENCY]


@pytest.mark.parametrize("symptoms", [
    ["乏力、气短、自汗"],            # 扫描复现：页面切出来是一整串，修前推荐为空表
    ["乏力 气短　自汗"],          # 半角、全角空格连写
    ["乏力,气短，自汗"],              # 半角、全角逗号挤在一项里
    [" 乏力 ", "乏力、气短", "自汗、"],  # 前后空白、重复、末尾多一个顿号：去空白、去重
])
def test_整串当一项送_后端拆开后命中三个(client, admin, symptoms):
    assert _assist(client, admin, symptoms) == [QI_DEFICIENCY]


def test_混用逗号顿号_阳虚证命中三个而不是一个(client, admin):
    got = _assist(client, admin, ["畏寒，肢冷、腰膝酸软"])
    assert [(r["syndrome"], r["match_count"]) for r in got] == [("阳虚证", 3)]   # 修前只认出畏寒：命中 1


def _diag_split() -> str:
    """页面辨证表单的切词链：`.split(…)…filter(Boolean)`。"""
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderTcm()")
    body = source[start:source.index("\nasync function ", start + 1)]
    return re.search(r'new FormData\(e\.target\)\.get\("symptoms"\)(\.split\(.+?\)\.filter\(Boolean\));', body).group(1)


def test_页面切词与导诊台同一个正则():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    triage = core[core.index('$("#triage-form").onsubmit'):]
    triage_regex = re.search(r"\.split\((/[^/\n]+/)\)", triage).group(1)
    assert triage_regex == "/[，,、\\s]+/"
    assert _diag_split().startswith(f".split({triage_regex})")   # 修前是 /[,，]/：不认顿号与空白


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的切词")
def test_页面切词_顿号空格逗号都认():
    cases = ["乏力、气短、自汗", "乏力 气短　自汗", "畏寒，肢冷、腰膝酸软", " 乏力，，气短 "]
    script = (f"const split = (text) => text{_diag_split()};\n"
              "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(split)));\n")
    done = subprocess.run(["node", "-e", script, json.dumps(cases, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        ["乏力", "气短", "自汗"],          # 修前 ['乏力、气短、自汗']
        ["乏力", "气短", "自汗"],          # 修前 ['乏力 气短　自汗']
        ["畏寒", "肢冷", "腰膝酸软"],      # 修前 ['畏寒', '肢冷、腰膝酸软']
        ["乏力", "气短"],
    ]
