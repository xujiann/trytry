"""DRG 事前提示页的两列按出参的实际含义命名（P2-720，第十八批「字符串比较当数值」扫描 V4-5）。

候选组的 `match_score` 两个键名与含义错位：`diagnosis_hits` 是总匹配分（诊断关键词每个 10 分 + 手术关键词每个 20 分 +
最长命中词字数），`procedure_hits` 是最长命中词的字数——出参模型的注释写明了，改键名属破坏性变更（第 7 条）。页面
却按键名印成「诊断命中」「手术命中」：命中一个五字诊断词的组显示「诊断命中 15、手术命中 5」，没有手术的病例也显示
「手术命中 5」，编码员照着以为命中了 15 个诊断词、5 个手术词。

修法：列名改「匹配分」「最长命中词长」，并写明匹配分怎么算；出参不动。
"""
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js"


def test_事前提示候选组两列按实际含义命名():
    page = PAGE.read_text(encoding="utf-8")
    assert '"诊断命中", "手术命中"' not in page   # 修前：键名照印，总分被读成「诊断命中了几个词」
    assert '["编码", "MDC", "名称", "基准权重", "匹配分", "最长命中词长"]' in page
    assert "每命中一个主诊断关键词 10 分、一个主手术关键词 20 分，再加最长命中词的字数" in page


def test_出参的两个数就是匹配分与最长命中词长(client, admin):
    """钉住页面说明与后端算法同一口径：命中一个诊断词、无手术 → 匹配分 = 10 + 词长，最长命中词长 = 词长。"""
    group = client.post("/api/drgs/groups", headers=admin, json={
        "code": "P2720A", "name": "P2720 测试组", "base_weight": 1.0, "keywords": "P2720肺炎"})
    assert group.status_code == 201, group.text
    resp = client.post("/api/drgs/pre-check", headers=admin, json={"diagnosis": "社区获得性P2720肺炎"})
    assert resp.status_code == 200, resp.text
    row = next(c for c in resp.json()["candidates"] if c["code"] == "P2720A")
    assert row["match_score"] == {"diagnosis_hits": 10 + len("P2720肺炎"), "procedure_hits": len("P2720肺炎")}
