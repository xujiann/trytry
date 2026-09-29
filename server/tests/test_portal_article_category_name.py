"""居民端健康宣教的分类显示中文名，不再原样印 general / chronic（P2-781，第二十批「界面文案 vs 行为」扫描 M2-9；P2-72 /
P2-372 同族）。

居民端文章卡片原先 `esc(a.category || "健康科普")`，免登录的文章清单只出编码：管理端按下拉（综合 / 慢病 / 妇幼 / 传染病）
发布两篇，居民端标签直接印 general、chronic；用户手册写的又是另一套（健康常识 / 慢病防治 / 妇幼保健 / 中医养生）。
修法：出参补 `category_name`（与管理端下拉同一套字，同文件价格公示的 `category_name` 同一个做法），居民端显示它，手册改成
同一套分类。建稿是否只收这四类（闭集）是另一回事。
"""
import re
from pathlib import Path

from app.routers.education import ARTICLE_CATEGORY_NAMES

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_免登录清单出分类中文名(client, admin):
    for title, category in (("P2781 综合篇", "general"), ("P2781 慢病篇", "chronic"), ("P2781 自拟分类", "中医养生")):
        created = client.post("/api/education/articles", headers=admin, json={
            "title": title, "category": category, "content": "正文"})
        assert created.status_code == 201, created.text
        assert client.post(f"/api/education/articles/{created.json()['id']}/publish", headers=admin).status_code == 200
    rows = {r["title"]: r for r in client.get("/api/portal/health-articles").json()}
    assert rows["P2781 综合篇"]["category_name"] == "综合"   # 修前没有这个键，居民端印 general
    assert rows["P2781 慢病篇"]["category_name"] == "慢病"
    assert rows["P2781 自拟分类"]["category_name"] == "中医养生"   # 不在表里的原样给


def test_中文名与管理端下拉同一套_居民端显示中文名():
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    form = source[source.index('<form class="inline" id="article-form">'):]
    form = form[:form.index("</form>")]
    options = dict(re.findall(r'<option value="(\w+)">([^<]+)</option>',
                              re.search(r'<select name="category">(.*?)</select>', form, re.S).group(1)))
    assert options == ARTICLE_CATEGORY_NAMES
    assert '<span class="cat">${esc(a.category_name || "健康科普")}</span>' in (STATIC / "m" / "m.js").read_text(
        encoding="utf-8")   # 修前 esc(a.category …)
