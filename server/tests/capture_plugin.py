"""套件级响应字节捕获（治理用的临时工具，不进仓库）。

思路：给 app 装一个中间件，把**整个测试套件**跑出来的每一个响应按
(方法, 路由模板, 状态码) 记下字节。加契约前后各跑一次、逐项比对——
一次覆盖所有模块，不用每个模块手写一遍捕获脚本。

同一个 (方法,路径模板,状态码) 会被命中很多次，全部按序保留：
同一端点的不同分支（空列表 / 有数据 / 各种错误）正是要比对的东西。

用法：MEDPLAT_CAPTURE=/path/to/out.json pytest tests/ -q -p capture_plugin
"""
import json
import os
import re

_OUT = os.environ.get("MEDPLAT_CAPTURE")
_RECORDS: dict[str, list[str]] = {}
_MAX_PER_KEY = 40          # 同一 key 只留前 40 条，防止把内存和文件撑爆
_MAX_BODY = 20000          # 单条响应只留前 20KB（超长的比前缀足够发现形状变化）

# 随机与时间：两次运行本就不同，比对前归一化
_NORMALIZERS = [
    (re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?'), "<TS>"),
    (re.compile(r'"(access_token|refresh_token|csrf_token)":"[^"]+"'), r'"\1":"<TOK>"'),
    (re.compile(r'"(qr_token|bind_token|trade_no|request_id|token)":"[A-Za-z0-9_.:\-]{8,}"'),
     r'"\1":"<TOK>"'),
    # 短信验证码回显：每次随机
    (re.compile(r'"debug_code":"\d{4,8}"'), '"debug_code":"<CODE>"'),
    # 积分商城核销码：randbelow 生成的六位数，每次随机
    (re.compile(r'"verify_code":"\d{6}"'), '"verify_code":"<CODE>"'),
    # ↓ 以下三条是**实测噪声地板**后补的（2026-08-24）。补之前同一份代码跑两次
    #   有 76 个组合"有差异"，全是这三类，足以把真回归淹掉；补完 spd 七个模块
    #   的噪声地板全部归零。
    # 电子健康卡号：建档时随机生成，每次运行都不同
    (re.compile(r'EHC[0-9A-F]{10,}'), "<EHC>"),
    # 墙钟时间戳：上面那条 <TS> 只认 ISO 的 T 分隔形式，这里是空格分隔的
    #   "YYYY-MM-DD HH:MM[:SS]"（多处 strftime 出来的展示用时间）
    (re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?'), "<WALL>"),
    # 单实例 id：进程启动时随机
    (re.compile(r'vm-[0-9a-f]{6,}'), "<VM>"),
]


def _normalize(text: str) -> str:
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text


def pytest_configure(config):
    if not _OUT:
        return
    from starlette.types import Message

    from app.main import app

    @app.middleware("http")
    async def _capture(request, call_next):
        response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", None) or request.url.path
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)

        key = f"{request.method} {template} -> {response.status_code}"
        bucket = _RECORDS.setdefault(key, [])
        if len(bucket) < _MAX_PER_KEY:
            try:
                text = body[:_MAX_BODY].decode("utf-8")
            except UnicodeDecodeError:
                text = f"<binary {len(body)} bytes>"
            ct = response.headers.get("content-type", "")
            if ct.startswith("image/svg"):
                # 二维码的**内容**随随机令牌变（连字节数也变，实测过），
                # 逐字节比没有意义；留下 <svg> 开标签——尺寸与 viewBox 由二维码
                # 版本决定，令牌长度固定时它是稳定的，也是"响应还对不对"的实义部分。
                head = text.split(">", 2)[:2]
                text = ">".join(head) + ">"
            bucket.append(f"[{ct}] {_normalize(text)}")

        async def _replay() -> Message:
            yield body

        response.body_iterator = _replay()
        return response


def pytest_sessionfinish(session, exitstatus):
    if not _OUT:
        return
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(_RECORDS, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n[capture] {len(_RECORDS)} 个 (方法,路径,状态) 组合已写入 {_OUT}")
