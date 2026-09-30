"""关键词检索的两边都在库里转小写：罗马数字、全角字母原文照搜搜得到（P2-919，第二十五批「搜索与模糊匹配」扫描 J4-6）。

`keyword_like` 原先把关键词在 Python 里 `.lower()`、列在库里 `lower()`：Python 的 lower 会把 Ⅱ 转成 ⅱ、Ｃ 转成 ｃ，SQLite 与
C / POSIX 区域的 PG 只转 ASCII——两边转出来不是同一个串，原文照搜也落空（P2-66 之前开发库能命中，是那次改动带来的回退）。
修前实测（开发库）：知识库 `q='Ⅱ型糖尿病'` → `[]`、`'ＣＯＰＤ'` → `[]`；慢专病病种 `keyword='Ⅱ型糖尿病'` → `[]`。修后两边都在库里
转；ASCII 不分大小写照旧（P2-66 的用例照绿）。全角半角互认另议。
"""


def test_罗马数字与全角字母原文照搜搜得到(client, admin):
    made = client.post("/api/knowledge", headers=admin,
                       json={"category": "clinical_guideline", "title": "P2919 Ⅱ型糖尿病基层管理（ＣＯＰＤ合并）"})
    assert made.status_code == 201, made.text
    for q in ("Ⅱ型糖尿病", "ＣＯＰＤ合并", "p2919"):
        got = client.get("/api/knowledge", headers=admin, params={"q": q})
        assert got.status_code == 200, got.text
        assert made.json()["id"] in {e["id"] for e in got.json()}, q   # 修前前两个 []
    program = client.post("/api/spd/programs", headers=admin,
                          json={"code": "p2919_t2dm", "name": "P2919 Ⅱ型糖尿病", "category": "chronic"})
    assert program.status_code == 201, program.text
    got = client.get("/api/spd/programs", headers=admin, params={"keyword": "Ⅱ型糖尿病"})
    assert "p2919_t2dm" in {p["code"] for p in got.json()}   # 修前 []
