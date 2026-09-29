"""村医手册与移动端、后端对得上（P2-778，第二十批「界面文案 vs 行为」扫描 M2-7 / 「同一个数，多处口径」扫描 M1-10）。

手册原先四处过时：「待复核转诊：你发起的上转走到哪一步了」（卡片自 P2-532 起数的是等本机构审核的，村医恒为 0）；「佐证需先在
管理端或让居民在居民端传照片」（移动端早有「上传佐证」）；「超期未办的任务会自动标红并通知卫生院」（超期扫描只标状态、
全站广播一个总数，不按机构发消息）；「单子会经服务站→卫生院→县医院逐级审核」（ADR-0005 已收敛为村→乡→县三级）。
这里按代码钉住手册的这几句。
"""
from pathlib import Path

from app.spd.routers.referral import _NEXT

ROOT = Path(__file__).resolve().parents[2]
MANUAL = (ROOT / "docs" / "培训手册" / "村医手册.md").read_text(encoding="utf-8")
DOCTOR_JS = (ROOT / "server" / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


def _line(prefix: str) -> str:
    start = MANUAL.index(prefix)
    return MANUAL[start:MANUAL.index("\n- ", start + 1)]


def test_待复核转诊说的是等本机构审核的_不是自己发起的():
    card = _line("- **待复核转诊**")
    assert "等你所在机构审核" in card and "你发起的上转走到哪一步了；" not in card   # 修前写成「你发起的」


def test_上转审核链与后端一致():
    steps = [step for _to, step, _level in _NEXT.values()]
    assert "服务站审核" not in steps   # 判据自证：后端新单只走卫生院、县医院两步（ADR-0005）
    chain = MANUAL[MANUAL.index("**发起上转**"):MANUAL.index("**承接下转**")]
    assert "服务站" not in chain and "卫生院→县医院" in chain, chain   # 修前「服务站→卫生院→县医院」


def test_佐证在移动端卡片上传_超期不说通知卫生院():
    assert 'b("data-spd-evidence", "上传佐证")' in DOCTOR_JS   # 判据自证：移动端有上传入口
    assert "**上传佐证**" in MANUAL and "需先在管理端或让居民在居民端传照片" not in MANUAL
    assert "通知卫生院" not in MANUAL   # 超期只标状态、全站广播总数，不按机构发消息
