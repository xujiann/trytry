"""开了 PII 加密后，平台患者检索不再拿关键词去 LIKE 密文列（P2-916，第二十五批「搜索与模糊匹配」扫描 J4-1）。

`search_patients` 把 `Patient.id_card.like(f"%{v}%")` 不分开关 OR 进条件。开态下列里存的是 `pii1$<base64>$<64位hex>`：
关键词是「pii1$」的子串（`$`、`pii`……）时所有密文行都命中；数字串在 hex / base64 里随机出现，于是随机命中别人
（2 万条密文估算：按 4 位数字搜每 10 万份档案误中 60～70 份）；`X-Total-Count` 跟着撑大。`app/pii.py` 写明开态「降级
为仅全值命中」，慢专病两处（P1-25）早按开关分支。修后开态只走索引等值；关态照旧 LIKE，但排除开过又关回时的存量
密文行。守卫 `test_pii_query_point_guard` 同时收紧：模糊比较必须写在关态分支里。
"""
import pytest

from app.config import settings

CARDS = ("330782199101012916", "330782199202022916", "330782199303032916")


@pytest.fixture(scope="module")
def encrypted(client, admin):
    """开态建三份档案（证件号落密文），建完关回——每条用例自己决定开关。"""
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "pii_encryption_enabled", True)
    try:
        for n, card in enumerate(CARDS):
            made = client.post("/api/patients", headers=admin, json={"name": f"P2916 甲{n}", "id_card": card})
            assert made.status_code == 201, made.text
    finally:
        mp.undo()


def _names(client, admin, keyword):
    got = client.get("/api/patients", headers=admin, params={"keyword": keyword, "limit": 500})
    assert got.status_code == 200, got.text
    return sorted(p["name"] for p in got.json() if p["name"].startswith("P2916"))


def test_开态_密文串不再被模糊命中_全值照中(client, admin, encrypted, monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    assert _names(client, admin, "$") == []     # 修前三份全中
    assert _names(client, admin, "pii") == []   # 修前三份全中
    assert _names(client, admin, CARDS[1]) == ["P2916 甲1"]   # 全值照旧走索引命中
    assert _names(client, admin, "P2916 甲2") == ["P2916 甲2"]   # 姓名模糊不受影响


def test_关态_存量密文行不被LIKE命中_全值照中(client, admin, encrypted, monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", False)
    assert _names(client, admin, "$") == []     # 开过又关回：存量密文行照旧不参与 LIKE
    assert _names(client, admin, CARDS[0]) == ["P2916 甲0"]   # 全值照旧走索引命中
    made = client.post("/api/patients", headers=admin, json={"name": "P2916 乙", "id_card": "330782199404042916"})
    assert made.status_code == 201, made.text
    assert _names(client, admin, "19940404") == ["P2916 乙"]   # 关态明文行照旧中缀命中
