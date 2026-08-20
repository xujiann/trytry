# CURRENT_ARCHITECTURE.md — 系统架构现状图

> 县域医共体信息化平台（medplat）+ 全域慢专病全流程管理子系统（spd）
> 本文件描述系统的整体形态、运行拓扑与数据流，仅描述现状（AS-IS）。
> 配套地图：`MODULE_MAP.md`（模块）、`DATA_MODEL.md`（数据）、`API_MAP.md`（接口）、`DEPENDENCY_MAP.md`（依赖）、`TECH_DEBT.md`（技术债）。

---

## 1. 一句话概括

一个**单进程 FastAPI 单体**：Python 后端 47,777 行、原生 JS 免构建前端 9,531 行、测试 24,429 行；**246 张表 / 881 个 HTTP 端点 / 89 个路由文件 / 52 个迁移**；同源内嵌三套 SPA（管理端 + 居民端 + 医生移动端）；慢专病以"可装卸子系统"形态装在同一个进程、同一个数据库里。

## 2. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | 单 worker（`start.sh` 无 `--workers`） |
| ORM | SQLAlchemy 2.x（`Mapped`/`mapped_column`） | 单一 `Base` |
| 迁移 | Alembic | 双 head 分支（平台链 + spd 链） |
| 配置 | pydantic-settings | `MEDPLAT_` 前缀，`@lru_cache` 单例 |
| 数据库 | 开发 SQLite / 生产 PostgreSQL 16 | 由 `MEDPLAT_DATABASE_URL` 切换 |
| 分布式状态 | Redis 7（可选） | 未配置时退化为进程内存 |
| 前端 | 原生 JS，**无构建工具** | 6 个 `<script>` 顺序加载，全局作用域 |
| 认证 | 自制 JWT（HMAC-SHA256 / HMAC-SM3） | 不依赖 PyJWT |
| 国密 | 纯 Python SM3（仅散列/MAC） | 明确不实现 SM2/SM4 |

## 3. 运行拓扑

```
                         ┌─────────────────────────────────────────────┐
   浏览器/H5/小程序  ───▶ │  Uvicorn (单进程, 端口 8000)                   │
                         │  ┌──────────────────────────────────────┐   │
                         │  │ FastAPI app (app.main:app)            │   │
                         │  │  中间件: 安全头 → 请求日志 → 审计落库    │   │
                         │  │  ~90 路由 + register_spd(9 路由)       │   │
                         │  │  StaticFiles /static, /, /m, /m/doctor │   │
                         │  │  WebSocket /ws/notifications (进程内)   │   │
                         │  │  asyncio scheduler_loop (进程内协程)    │   │
                         │  └──────────────────────────────────────┘   │
                         └───────────┬───────────────────┬─────────────┘
                                     │                   │
                          ┌──────────▼──────┐   ┌────────▼─────────┐
                          │ PostgreSQL 16   │   │ Redis 7 (可选)    │
                          │ (246 张表)       │   │ 黑名单/锁定/限流/  │
                          │                 │   │ 任务锁            │
                          └─────────────────┘   └──────────────────┘
   外部通道(双态): SMS(console/http) · 微信(mock/official) · 外呼(manual/http) · 支付网关(Mock/待接)
```

**单点集中在进程内**：调度器、WebSocket 广播、API 指标、（未配 Redis 时的）令牌黑名单/防爆破/限流。多实例部署必须配 `MEDPLAT_REDIS_URL`，且 WebSocket 广播仍需接 Redis Pub/Sub（未实现）。

## 4. 启动序列（lifespan）

```
进程启动
  └─▶ Base.metadata.create_all(engine)          # 建表（不走 alembic）
      └─▶ 建 admin 用户（口令取 settings）
          └─▶ 14 段种子数据（幂等"只增不改"）
              字典 → ICD-10/药品 → 绩效指标 → 传染病目录 →
              审方规则50 → 慢病病种8 → DRG → 质控规则 →
              病历质控规则 → 会计科目 → spd 子系统种子
              └─▶ sync_registry（定时任务注册表进库）
                  └─▶ seed_builtin_roles + sync_permissions（反射路由登记权限点）
                      └─▶ asyncio.create_task(scheduler_loop())
```

> 风险：lifespan 无 try/except（仅 `finally: db.close()`），任一段种子异常 = 整站起不来；且此路径无锁、无健康检查宽限。详见 `TECH_DEBT.md`。

## 5. 三层中间件（执行顺序）

1. **安全响应头**：`nosniff` / `X-Frame-Options: DENY` / `no-referrer` / CSP `default-src 'self'`（含 `script-src 'unsafe-inline'`，无 HSTS）。
2. **结构化 JSON 请求日志**：记 method/path/status/耗时（不记 query）。
3. **审计落库**：所有 `POST/PATCH/PUT/DELETE /api/*`（豁免 `/api/auth/login`）写 `audit_logs` 哈希链。每请求新开 Session、读上一条 entry_hash、insert+commit（**全局串行点**）。

## 6. 数据流（典型写请求）

```
Client ──JWT──▶ 中间件(安全头/日志)
              ──▶ 路由函数
                   ├─ get_current_user (deps): 验签→jti黑名单→scope→token基线
                   ├─ require_roles / require_admin (若有)
                   ├─ visibility.assert_*（若该路由接入了可见性）
                   ├─ 业务逻辑 + 直接 ORM（无 service 层，spd 部分例外）
                   ├─ concurrency.* 原子写（并发敏感处）
                   ├─ events.publish（仅 encounter.created / admission.discharged）
                   ├─ notify_* / ws.manager.broadcast（进程内）
                   └─ db.commit()
              ──▶ 审计中间件写哈希链
              ──▶ 响应（14% 有 response_model，其余裸 dict/list）
```

## 7. 认证与身份

- **两套身份**共用同一把密钥、同一 token 格式，靠 `scope` 双向拒绝：
  - 员工端（`users`，用户名口令，TTL 8h）
  - 居民端（`resident_accounts`，验证码/微信，TTL 7d，`scope=portal`）
- **6 内置角色**：admin / director / doctor / pharmacist / public_health / operator，叠加自定义角色权限点（仅覆盖写方法）。
- **横向隔离**（`visibility.py`）：机构维度（visible/stats/writable 三档）+ 患者维度（六类业务关系推导，patients 表本身无 org_id）。`director` 为全域可见角色。

## 8. 部署产物一览

| 产物 | 用途 | 现状要点 |
|---|---|---|
| `Dockerfile` / `server/Dockerfile` | 镜像 | 两份等价；`COPY . .` 无 .dockerignore；root 运行；无 HEALTHCHECK；默认 `SEED_DEMO=1` |
| `docker-compose.yml` | 生产一键起 | app+PG16+Redis7，健康检查规范；**无 alembic 步骤**；默认值触发守卫崩溃循环 |
| `render.yaml` | 演示部署 | 只设 `SEED_DEMO=1`，dev 密钥 + admin/admin123 + SQLite 无持久盘 |
| `.github/workflows/ci.yml` | CI | 只跑 pytest；覆盖率门禁带 `\|\| true`；不跑迁移/真PG/e2e/lint |
| `scripts/*` | 运维 | 备份(库+附件+密钥指纹)/恢复/恢复演练/legacy导入/字典导入/压测/演示灌数 |

## 9. 关键架构决策（代码中已文档化的取舍）

| 决策 | 位置 | 取舍说明 |
|---|---|---|
| 免构建 SPA | `core.js:6` | build-free 是既定约束，拆文件不上打包器 |
| 事件总线同步同事务 | `events.py` | 刻意不做异步队列/跨进程投递，订阅者共用 Session 只 add 不 commit |
| 金额用 Numeric 定点数 | `models.py:44` | 从 Float 迁移，PG/国产库精确求和 |
| 日期存 String(10) | `datetypes.py:15` | 改 DATE 会牵动 40+ 表迁移与全部字符串比较查询 |
| 状态用字符串不用 Enum | `spd/models.py:29` | 国产库 CHECK 差异 + 实施期口径易变 |
| SM2/SM4 不实现 | `gmcrypto.py:17` | "能跑但不安全的国密比没有更危险"，建议接硬件密码机 |
| spd 单向依赖 + 可装卸 | `spd/__init__.py` | 独立包/单向依赖/自成迁移分支，由 AST 测试守边界 |
| 调度器固定间隔非 cron | `scheduler.py:9` | 到期时刻落库，重启不漏跑 |

## 10. 边界与局限（现状已知）

- **单进程**：与 README 的多实例叙述矛盾；多实例需补 Redis 且 WebSocket 需 Pub/Sub。
- **create_all 与 alembic 双轨**：部署产物只走 create_all，迁移靠人工执行。
- **无领域/服务层**：业务逻辑内联在路由（spd 有局部 service 层）。
- **三套并行子域**：慢病 / 专病 / 慢专病概念重叠，存在数据孤岛。

*本文件仅描述现状，未对任何代码进行修改。*
