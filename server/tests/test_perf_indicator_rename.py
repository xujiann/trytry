"""共享诊断协同量：注释、口径文档与迁移告警还写着「两侧都计」，迁移让现场去改名的那一格界面上没有（P2-472）。

2026-08-27 计分回退为只计申请方、待卫健批复（`performance.org_scores` 的 docstring），可：

- `DEFAULT_INDICATORS` 的注释仍写「现在两侧都计」、说存量库的名字「由迁移 b5d9f3a71c2e 就地改」——那个迁移只报告不改；
- `docs/统计口径对照表.md` 只有 08-22「中心侧也计分」的裁定，没有回退这一笔；
- 迁移 b5d9f3a71c2e 的升级告警写「该维度自本轮起两侧都计」，并让现场到「绩效考核 → 指标目录」改名——
  接口 PATCH 一直收 name，页面上却只有调权重与启停。

修法：注释、文档、迁移告警对齐回退；「绩效指标调权」页补「改名」（名字给了就不能是空白）。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "server" / "app" / "static"


def _render_perf_indicators() -> str:
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderPerfIndicators()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_指标目录页有改名_走同一个PATCH():
    body = _render_perf_indicators()
    assert 'data-rename-ind="${esc(i.key)}" data-name="${esc(i.name)}">改名</button>' in body   # 修前没有这一格
    assert 'body: JSON.stringify({ name: form.name })' in body
    assert '{ name: "name", label: "指标名称（报表与考核明细里显示的名字）", value: d.name, required: true }' in body


def test_改名给了就不能是空白(client, admin):
    key = client.get("/api/performance/indicators", headers=admin).json()[0]["key"]
    before = next(i for i in client.get("/api/performance/indicators", headers=admin).json() if i["key"] == key)["name"]
    blank = client.patch(f"/api/performance/indicators/{key}", headers=admin, json={"name": "   "})
    assert blank.status_code == 422, blank.text   # 修前 200：报表与考核明细里这一维没有名字
    renamed = client.patch(f"/api/performance/indicators/{key}", headers=admin, json={"name": "P2472 新名"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "P2472 新名", renamed.text
    assert client.patch(f"/api/performance/indicators/{key}", headers=admin, json={"name": before}).status_code == 200


def test_口径文档补记08_27回退():
    doc = (ROOT / "docs" / "统计口径对照表.md").read_text(encoding="utf-8")
    section = doc[doc.index("**✅ 补充裁定（2026-08-22）：中心侧也计分，指标随之改名。**"):]
    assert "2026-08-27 回退待批" in section[:600]   # 紧跟在 08-22 裁定之后，读到裁定就读到回退


def test_迁移告警与注释不再说两侧都计():
    migration = (ROOT / "server" / "alembic" / "versions" / "b5d9f3a71c2e_rename_remote_exam_indicator.py")
    text = migration.read_text(encoding="utf-8")
    warning = text[text.index("logger.warning("):text.index("def upgrade()")]
    assert "两侧都计" not in warning   # docstring 里订正说明会引用原句，只查告警本身
    assert "「绩效考核 → 指标目录」直接改" not in text and "「绩效指标调权」页该行的「改名」" in text
    perf = (ROOT / "server" / "app" / "routers" / "performance.py").read_text(encoding="utf-8")
    assert "现在两侧都计" not in perf and "迁移 b5d9f3a71c2e 就地改" not in perf
    assert os.path.exists(migration)
