# 县域医共体信息化平台（medplat）

紧密型县域医共体信息化平台，依据国家卫生健康委《紧密型县域医共体信息化功能指引》（国卫办规划函〔2025〕63号）等文件建设。

- 建设规划：[县域医共体信息化平台建设规划.md](县域医共体信息化平台建设规划.md)
- 开发时间计划：[docs/开发时间计划.md](docs/开发时间计划.md)

## 当前进度

后端 + 管理端前端（免构建 SPA，启动后访问 `http://127.0.0.1:8000/`）均已就绪：

| 模块 | 说明 | 接口前缀 |
|---|---|---|
| 统一认证 | 登录、JWT 令牌、角色权限 | `/api/auth` |
| 机构管理 | 县-乡-村三级医共体成员单位 | `/api/organizations` |
| 患者主索引 | EMPI，电子健康卡号，幂等建档 | `/api/patients` |
| 统一编码字典 | "四统一"字典 + 批量导入 | `/api/dictionaries` |
| 双向转诊 | 上转/下转申请与状态流转 | `/api/referrals` |
| 就诊与健康档案 | 就诊记录、患者360视图 | `/api/encounters`、`/api/archive` |
| 共享诊断中心 | 影像/心电/检验/病理：基层检查、上级诊断、结果互认、危急值 | `/api/exams` |
| 集中审方 | "系统+药师"双重审方、用药规则库、事后处方点评 | `/api/prescriptions` |
| 中心药房 | 库存、余缺调拨、缺药预警、供应商/采购验收/盘点 | `/api/pharmacy` |
| 慢病管理 | 8 病种目录（分级规则/指导要点/随访周期）、建档、目录驱动智能分级、超期预警 | `/api/chronic`、`/api/chronic/disease-types` |
| 传染病监测 | 病例报告、多点触发预警 | `/api/infectious` |
| 决策驾驶舱 | 指标对齐《监测指标体系（2024版）》，含图表 | `/api/metrics` |
| 居民端 | 健康档案向本人开放（双因子核验） | `/api/portal` |
| 远程会诊 | 申请→受理→出具意见→评价 | `/api/consultations` |
| 家医签约 | 协议、服务包、履约记录 | `/api/contracts` |
| 预约诊疗 | 分时段号源发布与预约/取消/核销 | `/api/appointments` |
| 消毒供应 | 器械批次灭菌→发放→回收追溯 | `/api/cssd` |
| 医废追溯 | 收集→交接监管，超2天滞留预警 | `/api/medwaste` |
| 绩效考核 | 按机构自动汇算五维评分排名（限管理层） | `/api/performance` |
| 用户与审计 | 六类角色RBAC、账号管理、写操作审计留痕 | `/api/users`、`/api/audit` |
| 智慧急救 | 呼救调度、生命体征回传、上车即入院 | `/api/emergency` |
| 互联网+诊疗 | 在线咨询、复诊续方（联动审方） | `/api/telemedicine` |
| 中医药服务 | 智能辅诊/体质辨识、共享中药房追溯、适宜技术库 | `/api/tcm` |
| 药事监测 | 缺药登记、用药画像、多重用药预警、用药地图 | `/api/medication` |
| 医保协同 | 结算、转诊证明、特病申报、双通道药品申报、基金监测 | `/api/insurance` |
| 远程医学教育 | 课程、培训考核、直播申请审核、通过率统计 | `/api/education` |
| 老年健康 | ADL自动分级、失能清单、健康预警（重度失能/复评到期） | `/api/eldercare` |
| 妇幼保健 | 孕产妇建册/高危/访视、分娩记录、新筛与高危儿、妇女保健 | `/api/maternal` |
| 血液管理 | 血库台账、用血申请→审批→发血（库存拦截） | `/api/blood` |
| 法定证明与体检 | 出生/死亡医学证明、出生缺陷登记、成人体检异常清单 | `/api/certs`、`/api/checkups` |
| 统一知识库 | 药物政策/临床指南/转诊/质管制度/养生五分类，有效期管理 | `/api/knowledge` |
| 疫苗接种 | 接种登记、禁忌拦截、接种前评估 | `/api/vaccination` |
| 公卫协同 | 应急事件指挥、诊间医防提醒、五域卫生监测 | `/api/publichealth` |
| 综合管理 | 人力/派驻、财务集中核算、物资、公文、排班质控 | `/api/mgmt` |
| 对接适配层 | HL7 v2 ADT / FHIR R4 Patient·Observation 入站转换、FHIR 导出 | `/api/integration` |
| 实时消息 | WebSocket 危急值/缺药预警秒级广播、角色化任务待办中心 | `/ws/notifications`、`/api/todos` |
| 居民端移动版 | H5 移动优先：健康宣教、双因子查档案、满意度评价 | `/m` |
| 智能化 | 50 条规则库审方（剂量/相互作用/禁忌诊断/特殊人群/肝肾提示）、处方点评要点、慢病风险评分、药品采购建议 | `/api/prescriptions`、`/api/chronic/{id}/risk`、`/api/pharmacy/purchase-suggestions` |
| 附件服务 | 检查报告/不良事件附件：10MB限制、图片/PDF白名单、鉴权下载 | `/api/attachments` |
| 上报报表 | 监测指标14项当期值与CSV导出、运营月报CSV（限管理层） | `/api/reports` |

**《信息化功能指引》36项功能已全部实现**，逐项对照见 [docs/功能指引对照表.md](docs/功能指引对照表.md)。
第二阶段生产化演进（配置中心/迁移/安全合规/对接/实时消息/移动端/智能化）已完成首轮实现，详见 [docs/下一步开发计划.md](docs/下一步开发计划.md) 与 [docs/开发时间计划.md](docs/开发时间计划.md) 完成项勾选。

角色权限：诊断报告限医师、处方审核限药师、绩效考核限管理层、用户与审计限管理员（admin 全通）。

演示数据：启动服务后执行 `python scripts/seed_demo.py` 一键灌入。
对接规范：见 [docs/接口对接规范.md](docs/接口对接规范.md)（含 FHIR R4 资源映射）。

## 快速开始

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload      # 启动开发服务器，接口文档见 http://127.0.0.1:8000/docs
python -m pytest tests/ -q          # 运行测试
```

初始管理员账号：`admin` / `admin123`（生产部署前必须修改，并通过 `MEDPLAT_SECRET` 环境变量设置令牌密钥）。

数据库默认使用 SQLite（开发环境），通过 `MEDPLAT_DATABASE_URL` 环境变量可切换 PostgreSQL。
配置统一由 pydantic-settings 读取 `MEDPLAT_*` 环境变量（见 `server/app/config.py`）。

## 压测基线

不依赖 locust，`server/scripts/loadtest.py` 用 httpx + asyncio 并发压测三个核心接口
（登录 / 患者检索 / 检查开单），输出每场景 P50 / P95 / 平均耗时 / QPS：

```bash
cd server
uvicorn app.main:app --port 8000 &                                 # 先起服务（勿对生产库压测）
python scripts/loadtest.py http://127.0.0.1:8000 --concurrency 10 --requests 100
```

- 脚本自动用 admin 账号幂等准备一个机构与一名患者，开单场景会产生真实数据；
- 建议基线（本地 SQLite 单实例）：login P95 < 300ms、patients P95 < 100ms、
  exam_order P95 < 150ms；PostgreSQL + 多实例部署后重新采样并记录容量规划；
- 存量数据迁移见 `server/scripts/import_legacy.py`（CSV 批量导入，支持 `--dry-run`
  校验模式与错误行明细，样例见 `server/scripts/samples/`）；
- 标准字典导入见 `server/scripts/import_dictionary.py`（ICD-10 全量诊断/药品目录
  CSV 导入统一字典，`--dry-run` 与进度输出；启动种子已内置常用 ICD-10 诊断 100 条
  与常用药品 50 条，幂等入库）。

## 生产部署

```bash
docker compose up -d                          # 一键起 app + PostgreSQL 16 + Redis 7（命名卷持久化）
cd server && alembic upgrade head             # 结构迁移（与应用共用 MEDPLAT_DATABASE_URL）
sh scripts/backup.sh /data/backups            # pg_dump 备份（建议 crontab 定时）
```

### 生产环境安全硬化（必读）

- 设置 `MEDPLAT_ENVIRONMENT=prod`（或 `MEDPLAT_ENV=prod`）标识生产环境；
- **生产环境启动强校验**：`environment=prod` 时，若 `MEDPLAT_SECRET` 或
  `MEDPLAT_ADMIN_PASSWORD` 仍为默认值，应用启动直接抛异常拒绝启动，
  必须先设置强随机密钥（建议 `openssl rand -hex 32`）与强管理员口令；
- 多实例/多 worker 部署必须设置 `MEDPLAT_REDIS_URL`，使登出令牌黑名单与
  登录防爆破锁定跨实例共享（未配置时为进程内存实现，仅单实例有效）；
  WebSocket 预警广播在多实例下需接入 Redis Pub/Sub 等集中消息总线；
- WebSocket 客户端建议使用首帧鉴权（连接后第一条文本帧发送 JWT），
  避免令牌经 URL query 进入访问日志。
