"""审方规则导入的回执只把确实改了值的算「覆盖更新」，与改动记录同一个判法（P2-1665，第四十九批「集中审方与药事监测」扫描
AM3-7）。

`import_rules` 原先对已有编码不管有没有改动都 `updated += 1`，而改动记录按 `_log_rule_change` 的规矩「前后一模一样的不记」。
实测（修前）：同一条规则原值再导一遍，回执 `{'imported': 0, 'updated': 1}`，改动记录却只有当初「导入」那一条——重导整套
50 条规则，回执报「覆盖更新 50 条」，看不出真正改了哪几条。

修法：`_log_rule_change` 返回记没记，导入按它数 `updated`（不另写一份判法），一字未改的计进末尾新加的 `unchanged`；页面回执
跟着印「未改 N 条」。`updated` 因此变小是纠错（回执说的是「覆盖更新」）。
"""
from pathlib import Path

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")
CODE = "P1665-RULE"
RULE = {"drug_code": CODE, "max_daily_dose": 10, "dose_unit": "mg", "review_points": "P1665 核对疗程"}


def _import(client, admin, rows: list[dict]) -> dict:
    resp = client.post("/api/prescriptions/rules/import", headers=admin, json=rows)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _changes(client, admin) -> list[str]:
    resp = client.get(f"/api/prescriptions/rules/{CODE}/changes", headers=admin)
    assert resp.status_code == 200, resp.text
    return [c["action_name"] for c in resp.json()]


def test_回执三种计数_同值重导记未改_改了一项记覆盖更新(client, admin):
    first = _import(client, admin, [RULE])
    assert list(first) == ["imported", "updated", "unchanged"]   # 新键只在末尾
    assert first == {"imported": 1, "updated": 0, "unchanged": 0}   # 新编码照旧计 imported
    same = _import(client, admin, [RULE])
    assert same == {"imported": 0, "updated": 0, "unchanged": 1}   # 修前 {'imported': 0, 'updated': 1}
    assert _changes(client, admin) == ["导入"]   # 改动记录本就不记同值重导：回执与记录对上了
    changed = _import(client, admin, [{**RULE, "max_daily_dose": 8}])
    assert changed == {"imported": 0, "updated": 1, "unchanged": 0}
    assert _changes(client, admin) == ["导入", "导入"]


def test_一批里新建_改了的_没改的各计各的(client, admin):
    receipt = _import(client, admin, [
        {**RULE, "max_daily_dose": 8},                               # 与上一条用例留下的值相同
        {**RULE, "drug_code": "P1665-NEW", "max_daily_dose": 1},     # 新编码
    ])
    assert receipt == {"imported": 1, "updated": 0, "unchanged": 1}


def test_页面回执印未改条数():
    assert "未改 ${r.unchanged} 条" in CORE   # 修前只印新建与覆盖更新
