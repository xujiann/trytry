"""审计日志防篡改哈希链（阶段十一）。

每条审计记录带 `prev_hash` 与 `entry_hash`，
`entry_hash = MAC(平台密钥, prev_hash + 本条内容)`。

**能力边界要说清楚**：这条链能发现"某条历史记录被改过"，因为改了之后它
往后的每一条都对不上。但它**拦不住有库权限且知道平台密钥的人重算整条链**。
真正的不可抵赖需要外部存证或只追加存储（WORM），那属于部署形态而非应用能力。
把它说成"审计不可篡改"是夸大，平台侧能保证的是"改过就看得出来"。

密钥（A1）：写入用 `signing_key("audit")`——平台密钥按用途派生的子密钥，
不再与 JWT 共用一把。校验按 `verification_keys("audit")` 多口径回退：
历史链段是原始 secret 直签的、轮换宽限期内还有 previous 签的段，
逐条按"该条能通过的口径"判真——否则升级/轮换会把整段存量链误判为篡改。
"""
import hmac
import json

from .gmcrypto import mac
from .security import signing_key, verification_keys

__all__ = ["anchor_mac", "anchor_mac_valid", "audit_entry_hash", "verify_chain"]


def _payload(prev_hash: str, username: str, method: str, path: str, status_code: int) -> bytes:
    return f"{prev_hash}|{username}|{method}|{path}|{status_code}".encode()


def audit_entry_hash(
    prev_hash: str, username: str, method: str, path: str, status_code: int
) -> str:
    """写入口径：一律当前密钥的 audit 派生子密钥。"""
    return mac(signing_key("audit"), _payload(prev_hash, username, method, path, status_code)).hex()


def _entry_hash_valid(entry) -> bool:
    """本条内容与哈希是否相符：任一候选密钥口径算得出存储值即为真。"""
    payload = _payload(
        entry.prev_hash, entry.username, entry.method, entry.path, entry.status_code
    )
    return any(
        hmac.compare_digest(mac(key, payload).hex(), entry.entry_hash)
        for key in verification_keys("audit")
    )


# ---------------------------------------------------------------------------
# 外部锚点（P1-21）：锚点文件自身的 MAC 链
#
# 库内哈希链的盲区是"末尾截断"——删掉最新的 N 条，链照样自洽。补法是定期把
# 链尾（id / entry_hash / 总行数）写到库外的锚点文件并可外发异机存证：截断后
# 锚点所指的行不在库里，对账即暴露。锚点文件本身也要成链（每行对上一行的 mac
# 再做 MAC），否则删改锚点行同样无痕。密钥用独立用途派生（audit_anchor），
# 与链内哈希隔离。同样要说清边界：拿到磁盘与平台密钥的人仍可重算整个锚点文件，
# 真正的保障来自 webhook 外发的异机副本——本地锚点链防的是"顺手删几行"。
# ---------------------------------------------------------------------------

_ANCHOR_PURPOSE = "audit_anchor"


def _anchor_payload(prev_mac: str, record: dict) -> bytes:
    """锚点行的签名内容：上一行 mac + 本行内容的定序 JSON（不含 prev_mac/mac 自身）。"""
    body = json.dumps(
        {k: v for k, v in record.items() if k not in ("prev_mac", "mac")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"{prev_mac}|{body}".encode()


def anchor_mac(prev_mac: str, record: dict) -> str:
    """写入口径：当前密钥的 audit_anchor 派生子密钥。"""
    return mac(signing_key(_ANCHOR_PURPOSE), _anchor_payload(prev_mac, record)).hex()


def anchor_mac_valid(prev_mac: str, record: dict, given_mac: str) -> bool:
    """校验一条锚点行：与链内哈希同样按多口径回退，轮换宽限期内旧行不误判。"""
    payload = _anchor_payload(prev_mac, record)
    return any(
        hmac.compare_digest(mac(key, payload).hex(), given_mac)
        for key in verification_keys(_ANCHOR_PURPOSE)
    )


def verify_chain(entries) -> dict:
    """校验一段连续的审计记录。

    `entries` 须按 id 升序。返回首个断链位置——**只报第一处**：
    链断之后后面全都对不上，把它们都列出来只会淹没真正的那一处。
    """
    prev = entries[0].prev_hash if entries else ""
    for entry in entries:
        if entry.prev_hash != prev:
            return {"valid": False, "broken_at": entry.id, "reason": "与上一条的哈希不衔接"}
        if not _entry_hash_valid(entry):
            return {"valid": False, "broken_at": entry.id, "reason": "本条内容与哈希不符"}
        prev = entry.entry_hash
    return {"valid": True, "broken_at": None, "reason": ""}
