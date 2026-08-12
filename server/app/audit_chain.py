"""审计日志防篡改哈希链（阶段十一）。

每条审计记录带 `prev_hash` 与 `entry_hash`，
`entry_hash = MAC(平台密钥, prev_hash + 本条内容)`。

**能力边界要说清楚**：这条链能发现"某条历史记录被改过"，因为改了之后它
往后的每一条都对不上。但它**拦不住有库权限且知道平台密钥的人重算整条链**。
真正的不可抵赖需要外部存证或只追加存储（WORM），那属于部署形态而非应用能力。
把它说成"审计不可篡改"是夸大，平台侧能保证的是"改过就看得出来"。
"""
from .config import settings
from .gmcrypto import mac

__all__ = ["audit_entry_hash", "verify_chain"]


def audit_entry_hash(
    prev_hash: str, username: str, method: str, path: str, status_code: int
) -> str:
    payload = f"{prev_hash}|{username}|{method}|{path}|{status_code}"
    return mac(settings.secret.encode(), payload.encode()).hex()


def verify_chain(entries) -> dict:
    """校验一段连续的审计记录。

    `entries` 须按 id 升序。返回首个断链位置——**只报第一处**：
    链断之后后面全都对不上，把它们都列出来只会淹没真正的那一处。
    """
    prev = entries[0].prev_hash if entries else ""
    for entry in entries:
        expected = audit_entry_hash(
            entry.prev_hash, entry.username, entry.method, entry.path, entry.status_code
        )
        if entry.prev_hash != prev:
            return {"valid": False, "broken_at": entry.id, "reason": "与上一条的哈希不衔接"}
        if entry.entry_hash != expected:
            return {"valid": False, "broken_at": entry.id, "reason": "本条内容与哈希不符"}
        prev = entry.entry_hash
    return {"valid": True, "broken_at": None, "reason": ""}
