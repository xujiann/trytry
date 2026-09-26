"""考核指标清单按病种筛在分页之后：总数是没筛的，管这个病种的指标排在第一页之后就整个看不见（P2-300）。

`GET /api/spd/indicators?program_code=` 原先先按 `offset / limit` 取一页，再在这一页里挑「不限病种或含这个病种」的——
`X-Total-Count` 是没筛的总数、页里少几条；与团队清单按病种筛（P2-177）、报告实例「我的」（P2-295）同形。

修法：先挑出编号、再交回库里分页。
"""
B = "/api/spd"


def _indicator(client, admin, code, program_codes):
    created = client.post(f"{B}/indicators", headers=admin, json={
        "code": code, "name": code, "data_source": "task", "object_type": "team", "program_codes": program_codes})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_按病种筛的总数与逐页取到的条数对得上_管这个病种的都取得到(client, admin):
    for n in range(3):   # 编号在前、只管糖尿病的三条
        _indicator(client, admin, f"p2300_dm_{n}", ["diabetes"])
    mine = _indicator(client, admin, "p2300_htn", ["hypertension"])
    rows, offset = [], 0
    while True:   # 按总数逐页取（库里别的用例建的指标不影响判定）
        got = client.get(f"{B}/indicators", params={"object_type": "team", "program_code": "hypertension",
                                                    "active": True, "limit": 2, "offset": offset}, headers=admin)
        assert got.status_code == 200, got.text
        total = int(got.headers["X-Total-Count"])
        rows += got.json()
        offset += 2
        if offset >= total:
            break
    assert mine in [i["id"] for i in rows]
    assert len(rows) == total   # 修前总数是没筛的（多算了糖尿病那三条），逐页取到的又少几条
    assert all(not i["program_codes"] or "hypertension" in i["program_codes"] for i in rows)
