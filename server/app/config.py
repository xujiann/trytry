"""统一配置中心：pydantic-settings 读取 MEDPLAT_* 环境变量。

- MEDPLAT_DATABASE_URL   数据库连接串（默认本地 SQLite，生产用 PostgreSQL）
- MEDPLAT_SECRET         JWT 签名密钥
- MEDPLAT_ADMIN_PASSWORD 初始管理员密码
- MEDPLAT_ENV            环境标识 dev/test/prod（兼容保留）
- MEDPLAT_ENVIRONMENT    环境标识 dev/test/prod（与 MEDPLAT_ENV 任一为 prod 即按生产处理）
- MEDPLAT_LOG_JSON       是否输出结构化 JSON 请求日志

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
    secret: str = DEFAULT_SECRET
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    env: str = "dev"
    # 环境标识（默认 dev）：与 env 二者任一为 prod 即视为生产环境
    environment: str = "dev"
    log_json: bool = True
    # JWT 有效期（秒）
    token_ttl_seconds: int = 8 * 3600

    @property
    def is_production(self) -> bool:
        return "prod" in (self.env, self.environment)

    @model_validator(mode="after")
    def _reject_default_credentials_in_prod(self) -> "Settings":
        """H4 整改：生产环境沿用默认密钥/默认管理员口令时拒绝启动。"""
        if self.is_production:
            problems = []
            if self.secret == DEFAULT_SECRET:
                problems.append("MEDPLAT_SECRET 仍为默认值")
            if self.admin_password == DEFAULT_ADMIN_PASSWORD:
                problems.append("MEDPLAT_ADMIN_PASSWORD 仍为默认值")
            if problems:
                raise RuntimeError(
                    "生产环境配置不安全，拒绝启动：" + "；".join(problems)
                    + "。请通过环境变量设置强随机密钥与强口令后重启。"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内单例：首次访问时读取环境变量（测试可在导入前设置）。"""
    return Settings()


settings = get_settings()
