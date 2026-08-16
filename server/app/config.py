"""统一配置中心：pydantic-settings 读取 MEDPLAT_* 环境变量。

- MEDPLAT_DATABASE_URL   数据库连接串（默认本地 SQLite，生产用 PostgreSQL）
- MEDPLAT_SECRET         JWT 签名密钥
- MEDPLAT_ADMIN_PASSWORD 初始管理员密码
- MEDPLAT_UPLOAD_DIR     附件本地存储目录（默认 uploads/，即 server/uploads/）
- MEDPLAT_ENV            环境标识 dev/test/prod（兼容保留）
- MEDPLAT_ENVIRONMENT    环境标识 dev/test/prod（与 MEDPLAT_ENV 任一为 prod 即按生产处理）
- MEDPLAT_LOG_JSON       是否输出结构化 JSON 请求日志
- MEDPLAT_SMS_PROVIDER   居民端短信通道 console/http（console 仅打日志，非生产回显验证码）
- MEDPLAT_WECHAT_PROVIDER 居民端微信通道 mock/official

安全硬化（H4）：environment/env 为 prod 时，若 secret 或 admin_password
仍为默认值，进程启动直接抛异常拒绝启动，防止令牌被离线伪造。

兼容说明：环境变量命名与第一阶段完全一致，不破坏既有部署。
"""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "dev-secret-change-in-production"
DEFAULT_ADMIN_PASSWORD = "admin123"


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
    # 审计哈希链 MAC 密钥：默认回落到 secret（兼容既有部署），但建议单独配置——
    # 与 JWT 签名密钥分离后，任一用途泄露不会同时让令牌可伪造且审计链可重算。
    audit_secret: str = ""
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    env: str = "dev"
    # 环境标识（默认 dev）：与 env 二者任一为 prod 即视为生产环境
    environment: str = "dev"
    log_json: bool = True
    # 监控指标导出：置 true 时开放 GET /metrics（Prometheus 文本格式，无鉴权，
    # 供内网 Prometheus 抓取）。默认关闭——它会暴露内部模块名与流量，须部署在
    # 内网/受网络策略保护时再开。多实例下各进程各导出各自计数（进程内口径）。
    metrics_export: bool = False
    # JWT 有效期（秒）
    token_ttl_seconds: int = 8 * 3600

    # ---------------- 居民端登录（手机号验证码 / 微信） ----------------
    # 居民端令牌有效期：移动端会话比业务端长，默认 7 天
    portal_token_ttl_seconds: int = 7 * 24 * 3600
    # 验证码位数与有效期
    sms_code_ttl_seconds: int = 300
    # 兼容开关：电子健康卡号+身份证号的旧核验接口（已被账号体系取代）。
    # 生产环境建议置 false 关闭，避免留一个免登录的证件号查询面。
    portal_legacy_verify: bool = True
    # 短信通道：console=不外发仅打日志（开发/演示），http=转发到短信网关
    sms_provider: str = "console"
    sms_gateway_url: str = ""
    sms_api_key: str = ""
    sms_sign_name: str = "县域医共体"
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

    @property
    def is_production(self) -> bool:
        # 归一化匹配：此前用 `"prod" in (env, environment)` 做元组成员判断，
        # 只有精确写 "prod" 才命中——运维写 "production"/"prd"/大小写混排时
        # 生产判定静默失效，防默认密钥的唯一闸门被一个拼写差异关掉。
        marks = {"prod", "production", "prd"}
        return any((value or "").strip().lower() in marks for value in (self.env, self.environment))

    @model_validator(mode="after")
    def _reject_default_credentials_in_prod(self) -> "Settings":
        """H4 整改：生产环境沿用默认密钥/默认口令或留着联调登录面时拒绝启动。"""
        if self.is_production:
            problems = []
            if self.secret == DEFAULT_SECRET:
                problems.append("MEDPLAT_SECRET 仍为默认值")
            if self.admin_password == DEFAULT_ADMIN_PASSWORD:
                problems.append("MEDPLAT_ADMIN_PASSWORD 仍为默认值")
            # 居民端登录面：mock 微信=无凭据开户，console 短信=验证码回显日志，
            # 证件号旧核验=免登录查询，任一在生产开着都是可利用的登录/查询面。
            if self.wechat_provider == "mock":
                problems.append("MEDPLAT_WECHAT_PROVIDER 仍为 mock（生产须用 official）")
            if self.wechat_provider == "official" and not self.wechat_appid:
                problems.append("MEDPLAT_WECHAT_PROVIDER=official 但未配置 MEDPLAT_WECHAT_APPID")
            if self.sms_provider == "console":
                problems.append("MEDPLAT_SMS_PROVIDER 仍为 console（生产须用 http 网关）")
            if self.portal_legacy_verify:
                problems.append("MEDPLAT_PORTAL_LEGACY_VERIFY 未关闭（生产须置 false）")
            if problems:
                raise RuntimeError(
                    "生产环境配置不安全，拒绝启动：" + "；".join(problems)
                    + "。请通过环境变量修正后重启。"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内单例：首次访问时读取环境变量（测试可在导入前设置）。"""
    return Settings()


settings = get_settings()
