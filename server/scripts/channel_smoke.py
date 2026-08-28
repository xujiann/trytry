"""四个外部通道的真连通冒烟（ROADMAP「本容器内做不完」条目的落地准备）。

用法（在拿到厂商测试账号的环境里跑，凭据经 MEDPLAT_* 环境变量注入）::

    cd server
    python scripts/channel_smoke.py                 # 按配置逐通道真握手
    python scripts/channel_smoke.py --channel wechat
    python scripts/channel_smoke.py --sms-phone 138xxxxxxxx   # 短信要真发一条

## 这个脚本"诚实"的三条底线

1. **不伪造**：没有凭据就是 SKIP（缺什么写什么），绝不用 Mock 冒充 PASS——
   本仓库四个通道各自有 Mock 与生产硬门，但 ROADMAP 登记的缺口是
   「没有一次真实握手记录」，Mock 绿了不算数；
2. **握手尽量无副作用**：微信握手= 拉 client access_token（验 appid+secret+IP
   白名单，不发消息）；支付握手= 签名拉当日流水（验 URL+HMAC 密钥，只读，
   不动钱）；短信没有无副作用的真握手（发出去就是一条真短信、计费且打扰人），
   所以**必须显式给 --sms-phone（收测号码）才发**，不给就 SKIP；
3. **结果可入档**：每通道输出 PASS/FAIL/SKIP + 一句可贴进验收记录的证据
   （HTTP 状态/errcode/异常类别）。全部尝试成功退出码 0，有失败 1，
   全部 SKIP（无凭据环境，如 CI）2。

电子证照通道：平台侧**尚无对接代码路径**（grep 无果，ROADMAP 登记的是
"待对接"）——本脚本如实报 NO-PATH，不假装能冒烟一个不存在的集成。

写成脚本而不是测试：真握手要外网与真凭据，测试套件永远不该依赖这两样；
tests/test_channel_smoke.py 只锁本脚本的"诚实形状"（无凭据必 SKIP、不引 Mock）。
"""
import argparse
import datetime
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.egress import signed_headers  # noqa: E402

_TIMEOUT = 10.0

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    channel: str
    status: str
    evidence: str


def smoke_sms(sms_phone: str | None) -> Result:
    """短信网关：真发一条到显式提供的收测号码（无副作用的真握手不存在）。"""
    if not settings.sms_gateway_url:
        return Result("短信网关", SKIP, "未配置 MEDPLAT_SMS_GATEWAY_URL")
    if not sms_phone:
        return Result(
            "短信网关", SKIP,
            "已配置网关但未提供 --sms-phone；发送即真短信（计费/打扰），拒绝擅发",
        )
    from app.sms import HttpGatewaySmsProvider

    provider = HttpGatewaySmsProvider(
        settings.sms_gateway_url, settings.sms_api_key, settings.sms_sign_name
    )
    ok = provider.send(sms_phone, "【连通冒烟】县域医共体平台短信通道握手测试，请忽略")
    if ok:
        return Result("短信网关", PASS, f"网关受理（2xx），请人工确认 {sms_phone[:3]}****{sms_phone[-2:]} 收到")
    return Result("短信网关", FAIL, "网关拒绝或不可达（详见上方 [SMS-HTTP] 日志行）")


def smoke_wechat() -> Result:
    """微信公众号：拉 client access_token——验 appid+secret+服务器 IP 白名单，不发消息。"""
    if settings.wechat_provider != "official" or not settings.wechat_appid:
        return Result(
            "微信公众号", SKIP,
            "未配置 official 通道（MEDPLAT_WECHAT_PROVIDER=official + APPID/SECRET）",
        )
    import httpx

    try:
        resp = httpx.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": settings.wechat_appid,
                "secret": settings.wechat_secret,
            },
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络层失败也是要入档的冒烟结论
        return Result("微信公众号", FAIL, f"到 api.weixin.qq.com 不可达：{type(exc).__name__}")
    if "access_token" in data:
        return Result("微信公众号", PASS, f"access_token 获取成功（expires_in={data.get('expires_in')}）")
    return Result(
        "微信公众号", FAIL,
        f"errcode={data.get('errcode')} errmsg={str(data.get('errmsg'))[:80]}"
        "（40013=appid 错，40125=secret 错，89503=服务器 IP 不在白名单）",
    )


def smoke_payment() -> Result:
    """支付网关：HMAC 签名拉当日流水——验 URL 与密钥，只读不动钱。"""
    if not settings.payment_gateway_url:
        return Result("支付网关", SKIP, "未配置 MEDPLAT_PAYMENT_GATEWAY_URL / _KEY")
    import httpx

    today = datetime.date.today().isoformat()
    headers = signed_headers(settings.payment_gateway_key, b"")  # GET 无体，对空串签名（与对账同口径）
    try:
        resp = httpx.get(
            f"{settings.payment_gateway_url.rstrip('/')}/transactions",
            params={"date": today},
            headers=headers,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 同上，失败原因要入档
        return Result("支付网关", FAIL, f"网关不可达：{type(exc).__name__}")
    if resp.status_code in (401, 403):
        return Result("支付网关", FAIL, f"网关拒签（HTTP {resp.status_code}）——核对 MEDPLAT_PAYMENT_GATEWAY_KEY")
    if resp.status_code >= 400:
        return Result("支付网关", FAIL, f"HTTP {resp.status_code}：{resp.text[:80]}")
    try:
        json.loads(resp.text)
    except ValueError:
        return Result("支付网关", FAIL, "2xx 但应答非 JSON——URL 可能指到了别的服务")
    return Result("支付网关", PASS, f"流水接口应答正常（date={today}，HTTP {resp.status_code}）")


def smoke_ehcert() -> Result:
    """电子证照：平台侧尚无对接代码路径——如实报，不假装能冒烟。"""
    return Result(
        "电子证照", SKIP,
        "平台侧尚无电子证照对接代码（ROADMAP 登记为待对接）——先立对接任务，冒烟无从谈起",
    )


SMOKES = {
    "sms": lambda args: smoke_sms(args.sms_phone),
    "wechat": lambda args: smoke_wechat(),
    "payment": lambda args: smoke_payment(),
    "ehcert": lambda args: smoke_ehcert(),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", choices=[*SMOKES, "all"], default="all")
    parser.add_argument("--sms-phone", help="短信收测号码；不提供则短信通道 SKIP（拒绝擅发）")
    args = parser.parse_args(argv)

    names = list(SMOKES) if args.channel == "all" else [args.channel]
    results = [SMOKES[n](args) for n in names]

    width = max(len(r.channel) for r in results)
    print("\n== 外部通道真连通冒烟 ==")
    for r in results:
        print(f"  [{r.status}] {r.channel.ljust(width)}  {r.evidence}")
    print()
    if any(r.status == FAIL for r in results):
        return 1
    if all(r.status == SKIP for r in results):
        print("全部通道均无凭据可试（本容器/CI 的预期形态）；拿到厂商测试账号后在目标环境重跑。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
