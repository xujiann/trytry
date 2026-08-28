"""统一配置中心：pydantic-settings 读取 MEDPLAT_* 环境变量。

- MEDPLAT_DATABASE_URL   数据库连接串（默认本地 SQLite，生产用 PostgreSQL）
- MEDPLAT_SECRET         JWT 签名密钥
- MEDPLAT_ADMIN_PASSWORD 初始管理员密码
- MEDPLAT_UPLOAD_DIR     附件本地存储目录（默认 uploads/，即 server/uploads/）
- MEDPLAT_ENV            环境标识 dev/test/prod（兼容保留）
- MEDPLAT_ENVIRONMENT    环境标识 dev/test/prod（与 MEDPLAT_ENV 任一为 prod 即按生产处理）
- MEDPLAT_LOG_JSON       是否输出结构化 JSON 请求日志
- MEDPLAT_SMS_PROVIDER   居民端短信通道 console/http（console 仅打日志）
- MEDPLAT_SMS_DEBUG_ECHO  是否在响应回显 debug_code（默认关；须 console+非生产才生效）
- MEDPLAT_WECHAT_PROVIDER 居民端微信通道 mock/official

安全硬化（H4）：environment/env 为 prod 时，secret 与 admin_password 必须**足够强**，
否则进程启动直接抛异常拒绝启动，防止令牌被离线伪造。

判据不是"等于代码里的默认值"——那种写法只挡得住一个字符串。实测踩过：
`docker-compose.yml` 注入的是 `change-me-in-production`，而代码默认是
`dev-secret-change-in-production`，两者不相等，于是 `docker compose up`
（该文件里 `MEDPLAT_ENV: prod`）会带着一个**仓库里人人可见的签名密钥**正常启动。
一字符的密钥同样能过。现改为长度 + 字符多样性 + 占位符词表三重校验。

兼容说明：环境变量命名与第一阶段完全一致，不破坏既有部署。
"""
import logging
import os
from functools import lru_cache
from typing import ClassVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "dev-secret-change-in-production"
DEFAULT_ADMIN_PASSWORD = "admin123"

#: 生产环境的强度下限。密钥用于 HMAC 签名，短密钥可离线暴力破解后伪造任意令牌，
#: 故要求 32 字符（约当 128 bit 的十六进制/base64 随机串）；管理员口令 12 字符。
MIN_SECRET_LENGTH = 32
MIN_ADMIN_PASSWORD_LENGTH = 12

#: 字符多样性下限：挡住 "aaaa…a" 这种长度够、熵为零的值。
#: 不做真正的熵估算——那需要字典与语言模型，收益不抵复杂度；
#: "不同字符个数"是廉价且足够的代理指标。
MIN_SECRET_DISTINCT_CHARS = 8
MIN_ADMIN_PASSWORD_DISTINCT_CHARS = 6

#: 字符**类别**下限（小写/大写/数字/符号 四类中至少几类）。
#: 与"不同字符个数"是两回事：`123456789012` 有 9 种不同字符却只有一类，
#: 纯数字口令用最小的字符集，同样长度下可搜索空间小得多。
#: 只要 2 类——这是最低及格线，不是口令策略；把它抬高会开始误伤
#: 密码管理器生成的全小写+数字长串。
MIN_CHAR_CLASSES = 2

#: 占位符词表（大小写不敏感，子串匹配）。收录的是**真的在部署样例/文档里出现过**
#: 的措辞，不是通用弱口令字典——后者误伤随机串的概率反而更高。
PLACEHOLDER_MARKERS = (
    "change-me", "changeme", "change_me", "changethis", "change-this",
    "dev-secret", "your-secret", "your_secret", "yoursecret",
    "placeholder", "example", "todo", "xxxx",
    "admin123", "password", "passw0rd", "secret-key",
)
# 刻意**不**收 "123456" 这类纯数字串：它是经典弱口令，但作为**子串**信号太弱——
# 十六进制密钥里出现连续数字是正常的（64 位 hex 命中概率约百万分之三），
# 而误报的代价是运维拿着一个真随机密钥被拒之门外、还看不懂为什么。
# 词形占位符（change-me / placeholder / admin123 …）没有这个问题。


def _char_classes(value: str) -> int:
    """值里出现了几类字符（小写/大写/数字/符号）。"""
    return sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))


def _credential_problem(label: str, value: str, *, min_length: int, min_distinct: int) -> str | None:
    """返回该凭据不合格的原因；合格返回 None。"""
    lowered = value.lower()
    if len(value) < min_length:
        return f"{label} 长度 {len(value)} < {min_length}"
    if len(set(value)) < min_distinct:
        return f"{label} 仅含 {len(set(value))} 种字符（至少 {min_distinct} 种），熵过低"
    if _char_classes(value) < MIN_CHAR_CLASSES:
        return (
            f"{label} 只用了一类字符（纯数字/纯字母），"
            f"至少要 {MIN_CHAR_CLASSES} 类（小写/大写/数字/符号）"
        )
    for marker in PLACEHOLDER_MARKERS:
        if marker in lowered:
            return f"{label} 含占位符字样 {marker!r}，显然不是真正的随机值"
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDPLAT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./medplat.db"
    # 密码套件（阶段十一）：general=通用算法（PBKDF2/HMAC-SHA256），
    # sm=国密（SM3 迭代 / HMAC-SM3）。指引明文要求"密码应用"，但不该假定
    # 每个县都已上国密，故做成配置项而非代码分支。
    crypto_suite: str = "general"
    # 附件本地磁盘存储目录（相对服务运行目录 server/；生产可挂载持久卷）
    upload_dir: str = "uploads"
    secret: str = DEFAULT_SECRET
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    env: str = "dev"
    # 环境标识（默认 dev）：与 env 二者任一为 prod 即视为生产环境
    environment: str = "dev"
    log_json: bool = True
    # JWT 有效期（秒）
    token_ttl_seconds: int = 8 * 3600

    # ---------------- 居民端登录（手机号验证码 / 微信） ----------------
    # 居民端令牌有效期：移动端会话比业务端长，默认 7 天
    portal_token_ttl_seconds: int = 7 * 24 * 3600
    # 验证码位数与有效期
    sms_code_ttl_seconds: int = 300
    # 兼容开关：电子健康卡号+身份证号的旧核验接口（已被账号体系取代）。
    # **默认关**（TECH_DEBT P1-3）：免登录的证件号查询面不该默认敞开——
    # 默认开意味着每个没读过这行注释的部署都带着它上线。
    # 仍在过渡期、居民端账号未铺开的县显式置 true 开启。
    portal_legacy_verify: bool = False
    # 短信通道：console=不外发仅打日志（开发/演示），http=转发到短信网关
    sms_provider: str = "console"
    sms_gateway_url: str = ""
    sms_api_key: str = ""
    sms_sign_name: str = "县域医共体"
    # 验证码回显开关：仅当显式置 true 时，console 通道才在响应里回显 debug_code
    # （供本地联调/演示）。**默认关**——非生产的 staging/演示站也不再默认泄露验证码；
    # 生产环境即便置 true 也永不回显（见 routers/portal.py 的双重门）。
    sms_debug_echo: bool = False
    # 微信通道：mock=本地联调桩，official=微信公众平台网页授权
    wechat_provider: str = "mock"
    wechat_appid: str = ""
    wechat_secret: str = ""
    # 网页授权回调地址（须与公众号后台配置的域名一致）
    wechat_redirect_uri: str = ""

    # ---------------- 全域慢专病子系统（app/spd/） ----------------
    # 装卸开关：false 时不注册路由、不做种子化。表结构仍由迁移建出，
    # 只买基础平台的县不会在菜单里看到用不上的功能。
    spd_enabled: bool = True
    # 就诊事件触发专病纳入规则自动识别。**默认关闭**：全域自动识别会在
    # 生产上产生大量"疑似"记录，应由各县在诊断数据质量达标后再开启；
    # 关闭时仍可在管理端手工触发 `POST /api/spd/screenings/auto-run`。
    spd_auto_identify_on_encounter: bool = False
    # 出院事件自动生成随访计划。开着是安全的：只有配了诊断关键词的随访方案
    # 才会命中，没配关键词的方案不匹配任何人。
    spd_auto_followup_on_discharge: bool = True
    # 呼叫通道：manual=人工外呼（默认，建任务后由人打电话再回填结果），
    # http=对接呼叫中心网关（结果与录音由网关回调 result 接口）
    spd_call_provider: str = "manual"
    spd_call_gateway_url: str = ""

    # ---------------- 生产运行整改（A1-A12，阶段十三） ----------------
    # 密钥轮换宽限期：设置后，旧密钥仅用于**验证**既有令牌/审计链/动态码，
    # 新签发一律用当前 secret。轮换步骤见运维手册"密钥轮换"节。
    secret_previous: str = ""
    # PG 连接池（0=沿用 SQLAlchemy 默认；SQLite 忽略）。生产参考：
    # pool_size = worker 数 × 每 worker 并发 ÷ 实例数，超时给明确失败而非无限等。
    db_pool_size: int = 0
    db_max_overflow: int = 0
    db_pool_timeout_seconds: int = 0
    db_pool_recycle_seconds: int = 0
    # 患者可见性判定的进程内短 TTL 缓存（秒；0=关闭）。命中只复用"允许"结论，
    # 拒绝结论不缓存——授权收窄最迟 TTL 秒生效，放宽立即生效。
    visibility_cache_ttl_seconds: int = 30
    # 日志文件输出（空=仅 stdout；设路径则同时写轮转文件，配合等保 6 个月留存）
    log_file: str = ""
    log_rotate_max_mb: int = 64
    log_rotate_backups: int = 14
    # 运行数据保留期：任务运行记录按天清理；留痕/审计按天**归档导出后**截断
    # （0=不自动处理）。审计链截断会另记锚点，见 jobs.py。
    jobrun_retention_days: int = 90
    access_log_archive_days: int = 0
    audit_log_archive_days: int = 0

    # ---------------- 代码侧收口（两波工程包，阶段十四） ----------------
    # 任务/备份失败等运行告警的 webhook 出口（空=关闭）；同类告警冷却秒数
    alert_webhook_url: str = ""
    alert_cooldown_seconds: int = 600
    # 会话管理：空闲超时（秒，0=不启用滑动超时）；单账号并发会话上限（0=不限）
    session_idle_timeout_seconds: int = 0
    session_max_concurrent: int = 0
    # 双因素认证：要求 TOTP 的角色列表（逗号分隔，如 "admin,director"；空=关闭）
    totp_required_roles: str = ""
    # PII 列加密（SM4-CTR + HMAC-SM3 完整性 + HMAC 检索列）总开关。
    # 开启前须先跑回填迁移；密钥经 security.derive_key(info="medplat:pii") 派生。
    pii_encryption_enabled: bool = False
    # 支付网关（通用 HTTP 网关式，协议见 routers/billing.py 的 PaymentGateway）
    payment_gateway_url: str = ""
    payment_gateway_key: str = ""

    # ---------------- 收口轮登记项（P1-21/22） ----------------
    # 审计链外部锚点 webhook（空=仅本地锚点文件）：定期把链尾哈希外发异机存证，
    # 防"有库权限者末尾截断"——锚点在库外，删库内记录对不上锚点即暴露。
    audit_anchor_webhook_url: str = ""
    # ClamAV clamd 地址（host:port 或 unix:/path；空=不扫描，附件标 skipped）
    clamd_address: str = ""

    @property
    def is_production(self) -> bool:
        return "prod" in (self.env, self.environment)

    #: 「值写错就静默回落到默认实现」的枚举型配置项：配置名 → 合法取值。
    #:
    #: 这四项加上 crypto_suite 共用一个缺陷形状——选驱动的代码都写成
    #: `if 配置 == "某个值": 真实现` + `return 兜底实现`（见 sms.py:99、
    #: wechat.py、callcenter.py、gmcrypto.py:267）。于是把 `official` 敲成
    #: `offical`、把 `http` 敲成 `htpp`，**不报错、不告警**，直接退回桩件或
    #: 通用算法。其中微信那一条最狠：退回 mock 桩等于认证绕过。
    #: 拼写错误是配置里最常见的错，而这个形状把它变成了静默降级。
    _ENUM_SETTINGS: ClassVar[dict[str, tuple[str, ...]]] = {
        "crypto_suite": ("general", "sm"),
        "sms_provider": ("console", "http"),
        "wechat_provider": ("mock", "official"),
        "spd_call_provider": ("manual", "http"),
    }

    @model_validator(mode="after")
    def _reject_unknown_enum_values(self) -> "Settings":
        """枚举型配置项取值必须在清单内——**所有环境**都查，不只生产。

        开发环境同样会踩：本地把 provider 敲错、以为在测真通道、其实一直在测桩，
        这种"验错了对象"比生产事故更难发现。
        """
        problems = [
            f"MEDPLAT_{name.upper()}={getattr(self, name)!r} 不是合法取值"
            f"（合法值：{'、'.join(allowed)}）——写错不会报错，只会**静默**"
            f"退回默认实现"
            for name, allowed in self._ENUM_SETTINGS.items()
            if getattr(self, name) not in allowed
        ]
        if problems:
            raise RuntimeError("配置取值非法：" + "；".join(problems))
        return self

    @model_validator(mode="after")
    def _reject_weak_credentials_in_prod(self) -> "Settings":
        """生产环境凭据强度不足时拒绝启动。

        原实现只比对"是否等于代码里的默认值"，只挡得住一个字符串；
        换成长度/多样性/占位符三重校验（见 `_credential_problem`）。
        非生产环境不校验——本地开发就该能直接跑起来。
        """
        if not self.is_production:
            return self
        problems = [
            p for p in (
                _credential_problem(
                    "MEDPLAT_SECRET", self.secret,
                    min_length=MIN_SECRET_LENGTH,
                    min_distinct=MIN_SECRET_DISTINCT_CHARS,
                ),
                _credential_problem(
                    "MEDPLAT_ADMIN_PASSWORD", self.admin_password,
                    min_length=MIN_ADMIN_PASSWORD_LENGTH,
                    min_distinct=MIN_ADMIN_PASSWORD_DISTINCT_CHARS,
                ),
            ) if p
        ]
        if problems:
            raise RuntimeError(
                "生产环境配置不安全，拒绝启动：" + "；".join(problems)
                + "。请生成强随机值后经环境变量注入，例如："
                + "MEDPLAT_SECRET=$(python -c \'import secrets;print(secrets.token_hex(32))\')"
            )
        return self

    @model_validator(mode="after")
    def _reject_prod_footguns(self) -> "Settings":
        """生产守卫盲区（A2）：凭据强度之外还有两类"起得来但不该起"的配置。

        - 演示种子：`start.sh` 在 MEDPLAT_SEED_DEMO=1 时等服务就绪后自动执行
          `scripts/seed_demo.py`，向**运行中的库**灌入演示患者/机构/就诊。
          生产库被灌过一次就无法与真实数据干净剥离，只能整库重建，故直接拒启。
          该变量不是 Settings 字段（start.sh 的 shell 逻辑读它），这里直接读
          os.environ，与 MEDPLAT_REDIS_URL 同一处理方式。
        - SQLite：单文件库，多 worker 生产部署下并发写会锁库丢写，且迁移链
          只在 PostgreSQL 上验证过（见 §4/ADR-0002）。开发默认值一路带到生产
          是最容易犯的错，报错里把正确形态写明白。

        注意本 validator 排在凭据强度校验**之后**（pydantic 按定义顺序执行）：
        凭据弱时先报凭据——既有测试断言的就是那条报文。
        """
        if not self.is_production:
            return self
        problems = []
        if os.environ.get("MEDPLAT_SEED_DEMO", "").strip().lower() in ("1", "true", "yes", "on"):
            problems.append(
                "MEDPLAT_SEED_DEMO 已开启——它会向运行中的数据库灌入演示患者/机构数据，"
                "生产库灌入后无法与真实数据剥离；请移除该环境变量"
            )
        if self.database_url.strip().lower().startswith("sqlite"):
            problems.append(
                "MEDPLAT_DATABASE_URL 指向 SQLite——生产必须使用 PostgreSQL"
                "（如 postgresql://user:pass@host:5432/medplat），"
                "SQLite 无并发写能力且迁移链未在其上验证"
            )
        # Redis 口径（P1-26）：**多实例特征 + 无 Redis = 拒启；单实例无 Redis = 强警告**。
        # 取舍：单实例部署没 Redis 是合法形态（进程内存实现语义完整），一刀切拒启会
        # 误伤小站；但多实例下登出令牌黑名单、登录防爆破锁定、限流计数与任务执行锁
        # 全部退化为"各进程各一份"——已登出的令牌在别的 worker 仍可用，这是安全事故
        # 而非降级，不该只靠一条没人看的警告兜着。多实例判据取部署面上仅有的两个信号
        # （见 start.sh 与 docs/发布流程.md）：MEDPLAT_WORKERS>1（多 worker），或
        # MEDPLAT_MIGRATE_ON_START=0（多实例部署要求迁移由发布流程单独跑）。
        # 两个变量都是 start.sh 的 shell 口径而非 Settings 字段，与 MEDPLAT_SEED_DEMO
        # 同一处理方式直接读 os.environ。
        if not os.environ.get("MEDPLAT_REDIS_URL", ""):
            workers = os.environ.get("MEDPLAT_WORKERS", "").strip()
            multi_signals = []
            if workers.isdigit() and int(workers) > 1:
                multi_signals.append(f"MEDPLAT_WORKERS={workers}")
            if os.environ.get("MEDPLAT_MIGRATE_ON_START", "").strip() == "0":
                multi_signals.append("MEDPLAT_MIGRATE_ON_START=0")
            if multi_signals:
                problems.append(
                    f"检测到多实例部署特征（{'、'.join(multi_signals)}）但未配置 "
                    "MEDPLAT_REDIS_URL——多实例/多 worker 下登出令牌黑名单、"
                    "登录防爆破锁定、限流计数与定时任务执行锁均依赖 Redis 共享存储，"
                    "缺失即各进程各自为政（已登出令牌在其他实例仍可用）；"
                    "请配置 Redis（见 app/state_store.py 与运维手册第八节），"
                    "或确为单实例时移除该多实例配置"
                )
        # 通道配置：只拒启**无歧义的配置事故**，其余交给使用处硬门 + preflight 清单。
        # 判据沿用上面 Redis 那条的取舍——有确凿危险信号才拒启，合法形态只警告。
        if self.wechat_provider == "official" and not self.wechat_appid:
            problems.append(
                "MEDPLAT_WECHAT_PROVIDER=official 但 MEDPLAT_WECHAT_APPID 为空——"
                "这是配置事故，没有任何一种部署形态需要它：声明了走公众号却不给 appid，"
                "微信登录必然全部失败。请补齐 appid/secret/回调域名，"
                "或显式改回 MEDPLAT_WECHAT_PROVIDER=mock"
            )
        if problems:
            raise RuntimeError("生产环境配置不安全，拒绝启动：" + "；".join(problems))
        # 不拒启、只强警告：单实例部署没 Redis 是合法形态，多实例才是事故。
        # 进程起点只有这里一定会执行到，故警告放配置校验而非某个懒加载路径。
        if not os.environ.get("MEDPLAT_REDIS_URL", ""):
            logging.getLogger("medplat.config").warning(
                "生产环境未配置 MEDPLAT_REDIS_URL：登出令牌黑名单与登录防爆破锁定"
                "仅在本进程内存中生效——多实例/多 worker 部署下，已登出的令牌在"
                "其他实例仍可用、爆破计数互不相通。单实例可忽略本警告；"
                "多实例必须配置 Redis 共享存储（见 app/state_store.py）。"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内单例：首次访问时读取环境变量（测试可在导入前设置）。"""
    return Settings()


settings = get_settings()
