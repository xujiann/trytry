"""日志出口不得落 PII 与一次性口令。

**这条是被自己的改动逼出来的。** 把 handler 从 `medplat.access` 上移到 `medplat`
父层、级别设 INFO 之后，全部 18 个 logger 的 `logger.info` 第一次真的产生了记录——
其中 `sms.ConsoleSmsProvider.send` 那句 `logger.info("[SMS-CONSOLE] to=%s content=%s")`
就把**手机号和明文验证码**写进了 stdout 与等保 6 个月留存文件。

改动前它"看着没事"：`medplat.sms` 没有任何 handler，`getEffectiveLevel()` 落到 root 的
WARNING，`logger.info` 在建记录之前就被丢掉了。也就是说这行代码一直是错的，
只是被另一个缺陷盖住了——一个缺陷把另一个缺陷藏起来，修掉前者才让后者显形。

要紧的是两条口径：
- 验证码在库里**只落散列**（`routers/portal.py`「验证码只落散列」），日志里落明文
  等于绕开那条设计——攻击者拿到日志就等于拿到了一次性口令；
- `docs/运维手册.md` 写着日志「不含请求体，不会落身份证号/电话等敏感字段」，
  代码得对得上这句话（CLAUDE.md §4 出口一律经 privacy.py 脱敏、§8 安全红线）。
"""
import io
import logging

import pytest

from app import sms, wechat
from app.config import settings
from app.main import _MedplatFormatter
from app.privacy import mask_phone

PHONE = "13900000000"
CODE = "481203"
CONTENT = f"【县域医共体】验证码 {CODE}，5分钟内有效，请勿转发。"


@pytest.fixture
def captured(monkeypatch):
    """把 `medplat` 父层的输出抓进内存，还原真实的落盘/落 stdout 路径。"""
    buf = io.StringIO()
    root = logging.getLogger("medplat")
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_MedplatFormatter())
    old_level, old_handlers = root.level, root.handlers[:]
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    yield buf
    root.handlers = old_handlers
    root.setLevel(old_level)


def _send_sms(monkeypatch, *, debug_echo: bool, production: bool):
    monkeypatch.setattr(settings, "sms_debug_echo", debug_echo)
    monkeypatch.setattr(type(settings), "is_production", property(lambda _s: production))
    assert sms.ConsoleSmsProvider().send(PHONE, CONTENT) is True


# ---------------------------------------------------------------- 短信

def test_生产日志不落验证码(captured, monkeypatch):
    """核心：验证码是一次性口令，落进 6 个月留存文件等于把口令存了 6 个月。"""
    _send_sms(monkeypatch, debug_echo=False, production=True)
    out = captured.getvalue()
    assert CODE not in out, f"验证码明文进了日志：{out}"
    assert CONTENT not in out


def test_生产日志不落完整手机号(captured, monkeypatch):
    _send_sms(monkeypatch, debug_echo=False, production=True)
    out = captured.getvalue()
    assert PHONE not in out, f"完整手机号进了日志：{out}"
    # 掩码形态从 privacy.mask_phone 现算，不写死星号个数——写死的话
    # 哪天脱敏口径调整，这条会因为一个无关的差异变红。
    assert mask_phone(PHONE) in out, f"应留掩码后的号码（privacy.mask_phone 口径）：{out}"


def test_开关开着但在生产仍不落明文(captured, monkeypatch):
    """开关只是本地联调用的，误开到生产也不能把明文放出去——
    与 `routers/portal.py` 回显 debug_code 的双重门同一口径。"""
    _send_sms(monkeypatch, debug_echo=True, production=True)
    assert CODE not in captured.getvalue()


def test_非生产且显式开开关才打明文(captured, monkeypatch):
    """本地联调得看得见验证码，否则 console 通道就没用了。"""
    _send_sms(monkeypatch, debug_echo=True, production=False)
    assert CODE in captured.getvalue()


def test_非生产但开关关着也不打明文(captured, monkeypatch):
    """默认关：开发机的日志同样会被拷来拷去。"""
    _send_sms(monkeypatch, debug_echo=False, production=False)
    assert CODE not in captured.getvalue()


def test_仍然记下发生过一次下发(captured, monkeypatch):
    """脱敏不等于不记——排障要看得出"到底发没发出去"。"""
    _send_sms(monkeypatch, debug_echo=False, production=True)
    assert "[SMS-CONSOLE]" in captured.getvalue()


# ---------------------------------------------------------------- 微信模板消息

def test_微信桩不落模板字段的值(captured):
    """模板消息的 value 里是姓名、就诊时间这类居民信息；打字段名足够联调。"""
    wechat.MockWeChatProvider().send_template_message(
        "mock_abc", "tpl-1", {"first": {"value": "张三，明天上午复诊"}, "keyword1": {"value": PHONE}}
    )
    out = captured.getvalue()
    assert "张三" not in out, f"居民姓名进了日志：{out}"
    assert PHONE not in out
    assert "first" in out and "keyword1" in out, f"字段名应当留下，否则联调看不出模板对不对：{out}"


# ---------------------------------------------------------------- 形状守卫

def test_没有别的地方把整条短信正文打进日志():
    """AST 防复发：`content` 整个进 logger 参数的写法只允许出现在那条受双重门保护的分支里。"""
    import ast
    import pathlib

    src = pathlib.Path(sms.__file__).read_text("utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `exception` 必须在集合里——第一版漏了它，恰好放走了 HttpGateway
        # 失败分支里那句 `logger.exception(... phone=%s", phone)`：
        # 失败路径的 ERROR 恰恰是最会被留存、被贴进工单的日志。
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in {"info", "warning", "error", "debug", "exception", "critical"}
        ):
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Name) and arg.id in {"content", "phone"}:
                offenders.append((node.lineno, arg.id))
    # 允许且仅允许一处：debug_echo + 非生产 那一支里的 (phone, content)
    assert len(offenders) == 2 and {a for _, a in offenders} == {"phone", "content"}, (
        f"sms.py 里把 phone/content 直接交给 logger 的地方不止受门保护的那一处：{offenders}"
    )
    guarded_line = offenders[0][0]
    guard = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and n.lineno < guarded_line
        and any(getattr(c, "lineno", -1) == guarded_line for c in ast.walk(n))
    ]
    assert guard, "那一处没有被 if 包住——双重门不见了"
    guard_src = ast.unparse(guard[-1].test)
    assert "sms_debug_echo" in guard_src and "is_production" in guard_src, (
        f"双重门缺了一半（须同时要求显式开关与非生产）：{guard_src}"
    )
