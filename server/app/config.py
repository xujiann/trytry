"""统一配置中心：pydantic-settings 读取 MEDPLAT_* 环境变量。

- MEDPLAT_DATABASE_URL   数据库连接串（默认本地 SQLite，生产用 PostgreSQL）
- MEDPLAT_SECRET         JWT 签名密钥
- MEDPLAT_ADMIN_PASSWORD 初始管理员密码
- MEDPLAT_ENV            环境标识 dev/test/prod
- MEDPLAT_LOG_JSON       是否输出结构化 JSON 请求日志

兼容说明：环境变量命名与第一阶段完全一致，不破坏既有部署。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDPLAT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./medplat.db"
    secret: str = "dev-secret-change-in-production"
    admin_password: str = "admin123"
    env: str = "dev"
    log_json: bool = True
    # JWT 有效期（秒）
    token_ttl_seconds: int = 8 * 3600


@lru_cache
def get_settings() -> Settings:
    """进程内单例：首次访问时读取环境变量（测试可在导入前设置）。"""
    return Settings()


settings = get_settings()
