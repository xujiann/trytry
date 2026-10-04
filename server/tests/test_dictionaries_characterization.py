"""特征化测试——保护 dictionaries 的 /import 从裸 dict 迁移到 response_model。

dictionaries 其余端点已有契约（CodeEntryOut），只剩批量导入返回裸
`{"imported", "skipped"}`。迁移目标是加 response_model，响应字节必须不变
（CLAUDE.md 第7条）。本测试钉住迁移前的精确键与计数语义，迁移后仍须全绿。
"""
from __future__ import annotations

from conftest import login


#: 后三键是 P2-1271 只增不改加的（跳过的拆成「库里已有」与「本批重复」、点名本批重复的编码）；前两键的计数语义不变
IMPORT_KEYS = {"imported", "skipped", "skipped_existing", "skipped_duplicate", "duplicate_codes"}


def _admin(client):
    return login(client, "admin", "admin123")


def test_import_keys_and_counts(client):
    admin = _admin(client)
    # 用不会被启动种子占用的自造编码，避免与预置字典撞码
    body = [{"code": "ZZTEST01", "name": "特征化诊断一"},
            {"code": "ZZTEST02", "name": "特征化诊断二"}]
    first = client.post("/api/dictionaries/diagnosis/import", json=body, headers=admin)
    assert first.status_code == 200, first.text
    assert set(first.json().keys()) == IMPORT_KEYS
    assert first.json() == {"imported": 2, "skipped": 0,
                            "skipped_existing": 0, "skipped_duplicate": 0, "duplicate_codes": []}

    # 再导：已存在的跳过，新增的计入
    body2 = body + [{"code": "ZZTEST03", "name": "特征化诊断三"}]
    again = client.post("/api/dictionaries/diagnosis/import", json=body2, headers=admin).json()
    assert set(again.keys()) == IMPORT_KEYS
    assert again == {"imported": 1, "skipped": 2,
                     "skipped_existing": 2, "skipped_duplicate": 0, "duplicate_codes": []}
