"""机构协作分组页「按机构查归属」只读 of-org 行里真有的键（P2-461）。

`GET /api/org-groups/of-org/{org_id}` 的行按设计**不带成员数**（`OrgGroupOut`：键集合按端点固定，
创建/列表恒带 member_count，更新/of-org 恒不带）。原先这块的表格照抄了上面分组列表的模板，
读 `g.member_count`，成员数一列对每个分组都显示 undefined。

现在成员数从本页已取的分组列表按 id 对上。本用例从页面源码里取出这段提交处理，
把行渲染回调读的键与响应模型逐个比对——哪天再读一个 of-org 不返回的键，这里就红。
"""

import re
from pathlib import Path

from app.routers.org_groups import OrgGroupOut

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js"


def _of_org_handler() -> str:
    src = PAGE.read_text(encoding="utf-8")
    start = src.index('$("#og-oforg").onsubmit')
    end = src.index("\n  };", start)
    return src[start:end]


def test_of_org_rows_only_read_keys_the_endpoint_returns():
    handler = _of_org_handler()
    assert "/api/org-groups/of-org/" in handler
    assert re.search(r"\brows,\s*\(g\)\s*=>", handler), "行渲染回调换了写法，本用例要跟着改"
    read = set(re.findall(r"\bg\.(\w+)", handler))
    missing = read - set(OrgGroupOut.model_fields)
    assert not missing, f"of-org 行没有这些键，表格上会显示 undefined：{sorted(missing)}"
