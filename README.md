# 县域医共体信息化平台（medplat）

![tests](https://img.shields.io/badge/tests-363%20passed%20%2B%204%20e2e-brightgreen)
![coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)

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
| 决策指标扩展 | 县外就诊登记（补齐县域就诊率/外转率数据源）、就医流向与有序转诊率、运行效率（平均住院日/床位周转/使用率/医师担负）、自定义绩效公式（AST白名单求值）与期末综合报告 | `/api/analytics` |
| 决策驾驶舱 | 指标对齐《监测指标体系（2024版）》，含图表 | `/api/metrics` |
| 居民端 | 手机号验证码/微信登录 + 实名绑定、家庭成员代管、自助预约、我的签约/账单/转诊 | `/api/portal` |
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
| 会计核算 | 会计科目（22条种子）、记账凭证借贷平衡强校验、过账锁定与作废、试算平衡表 | `/api/accounting` |
| 成本核算 | 科室直接成本归集、分摊规则（行政/医技→临床）、诊次成本与床日成本（分母为实际占用床日） | `/api/cost` |
| 物资采购与耗材 | 非药品物资申请→审批→合同→验收（自动入库流水）、高值耗材一物一码正反向追溯 | `/api/materials` |
| 综合管理 | 人力/派驻、财务集中核算、物资、公文、排班质控 | `/api/mgmt` |
| 住院临床文书 | 病程记录（首次/日常/查房/抢救）、护理记录、体温单（体征时序）、交接班；出院前文书完整性自查 | `/api/inpatient/admissions/{id}/progress-notes` 等 |
| 手术麻醉 | 申请→审批（申请人不得自批）→手术间排班（区间重叠拦截）→术中记录；病案首页手术栏自动取术式 | `/api/surgery` |
| 随访中心 | 慢病/出院/术后/妇幼四类随访统一任务模型，出院与手术结案自动派生 | `/api/followups` |
| 病历环节质控 | 结构化病历（主诉/现病史/既往史/查体/诊断依据/治疗方案）提交即评分，12 条规则实时缺陷清单与甲乙丙定级 | `/api/quality/records` |
| 统一支付与对账 | 多渠道支付单（现金/银行卡/医保/线上，PaymentGateway 协议）、退款校验、日终对账三类差异检出 | `/api/billing/payments`、`/api/billing/reconciliation` |
| 对接适配层 | HL7 v2 ADT / FHIR R4 Patient·Observation 入站转换、FHIR 导出 | `/api/integration` |
| 集成平台 ESB | 接入方注册（令牌+限流）、消息队列重试与死信、流程编排（转换/校验/路由/落库）、成功率与积压统计 | `/api/esb` |
| 定时任务 | 任务注册表、间隔调度、多实例抢锁、手动触发与执行留痕；内置慢病超期/医废滞留/合同到期/制剂临期/验证码清理 | `/api/jobs` |
| 实时消息 | WebSocket 危急值/缺药预警秒级广播、角色化任务待办中心 | `/ws/notifications`、`/api/todos` |
| 居民端移动版 | H5 移动优先：微信/手机号验证码登录、实名绑定、家庭成员代管与档案切换、自助预约、签约/账单/转诊查询、健康宣教、满意度评价 | `/m` |
| 医生移动工作台 | H5 移动优先：待办收件箱、危急值确认与处置、待审检查申请领取出报告、慢病随访录入、患者档案速查 | `/m/doctor` |
| 智能化 | 50 条规则库审方（剂量/相互作用/禁忌诊断/特殊人群/肝肾提示）、处方点评要点、慢病风险评分、药品采购建议 | `/api/prescriptions`、`/api/chronic/{id}/risk`、`/api/pharmacy/purchase-suggestions` |
| 附件服务 | 检查报告/不良事件附件：10MB限制、图片/PDF白名单、鉴权下载 | `/api/attachments` |
| 上报报表 | 监测指标14项当期值与CSV导出、运营月报CSV（限管理层） | `/api/reports` |

**《信息化功能指引》36项功能已全部实现**，逐项对照见 [docs/功能指引对照表.md](docs/功能指引对照表.md)。
第二阶段生产化演进（配置中心/迁移/安全合规/对接/实时消息/移动端/智能化）已完成首轮实现，详见 [docs/下一步开发计划.md](docs/下一步开发计划.md) 与 [docs/开发时间计划.md](docs/开发时间计划.md) 完成项勾选。
第三轮全量代码核对（对照国家指引、浙江省指南、监测指标体系）与后续排期见 [docs/第三轮全量核对与下一步开发计划.md](docs/第三轮全量核对与下一步开发计划.md)。

角色权限：诊断报告限医师、处方审核限药师、绩效考核限管理层、用户与审计限管理员（admin 全通）。

演示数据：启动服务后执行 `python scripts/seed_demo.py` 一键灌入。
对接规范：见 [docs/接口对接规范.md](docs/接口对接规范.md)（含 FHIR R4 资源映射）。

## 快速开始

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload      # 启动开发服务器，接口文档见 http://127.0.0.1:8000/docs
python -m pytest tests/ -q          # 运行测试（端到端用例默认跳过）
```

初始管理员账号：`admin` / `admin123`（生产部署前必须修改，并通过 `MEDPLAT_SECRET` 环境变量设置令牌密钥）。

### 居民端（`/m`）怎么登录

居民端**没有密码**，走手机号验证码或微信授权：

1. 打开 `/m` → 「我的档案」；
2. 点「微信一键登录」（默认 mock 通道，无需公众号即可走通），或填手机号点「获取验证码」——
   开发/演示环境（`MEDPLAT_SMS_PROVIDER=console` 且非 prod）验证码会直接回显并自动填入；
3. 首次登录需实名绑定：填姓名 + 身份证号匹配已建档的患者。若登录手机号已登记在
   某份档案上且**全库唯一**，则登录即自动完成绑定，跳过这步。

接入真实通道只需配环境变量，业务代码不动：`MEDPLAT_SMS_PROVIDER=http` +
`MEDPLAT_SMS_GATEWAY_URL`，`MEDPLAT_WECHAT_PROVIDER=official` + `MEDPLAT_WECHAT_APPID`/
`MEDPLAT_WECHAT_SECRET`/`MEDPLAT_WECHAT_REDIRECT_URI`。详见
[docs/接口对接规范.md](docs/接口对接规范.md) 附录C。

数据库默认使用 SQLite（开发环境），通过 `MEDPLAT_DATABASE_URL` 环境变量可切换 PostgreSQL。
配置统一由 pydantic-settings 读取 `MEDPLAT_*` 环境变量（见 `server/app/config.py`）。

## 测试与覆盖率

```bash
cd server
python -m pytest tests/ -q                                        # 全量单元/接口测试（363 项通过，4 项 e2e 默认跳过）
python -m pytest tests/ -q --cov=app --cov-report=term-missing    # 附带覆盖率报告
```

- 当前行覆盖率 **93%**（阈值 70%），徽章值见文首；
- CI（`.github/workflows/ci.yml`）每次推送执行全量测试并产出 `coverage.xml`
  工件；覆盖率门禁当前为 **warning 模式**：低于 `COVERAGE_MIN`（70%）只告警不
  阻断构建，去掉门禁步骤的 `|| true` 即切换为强制门禁。

### 端到端测试（Playwright）

`server/tests/e2e/test_flows.py` 用真实浏览器驱动管理端 SPA，覆盖
**登录 → 决策驾驶舱 → 共享诊断中心开单 → 领取并出报告（危急值）→ 危急值确认接收
→ 处置反馈闭环**，以及医生移动工作台 `/m/doctor` 登录。用例标记 `@pytest.mark.e2e`，
默认跳过（无浏览器内核的环境不会失败），需显式开启：

```bash
cd server
pip install playwright                 # E2E 可选依赖，不在 requirements.txt 中
python -m playwright install chromium  # 首次需下载浏览器内核（约 150MB）
python -m pytest tests/e2e -q --e2e    # 仅跑端到端；--e2e 也可加在全量命令上
```

- 用例自动拉起独立 uvicorn 子进程与独立 SQLite 库（`e2e_run.db`），跑完即清理，
  不影响开发库与单元测试库；端口由内核动态分配，可与本地服务并存；
- 未传 `--e2e` 时由 `tests/conftest.py` 统一跳过，因此 CI 默认不需要浏览器内核。

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
- 多实例/多 worker 部署必须设置 `MEDPLAT_REDIS_URL`，使登出令牌黑名单、
  登录防爆破锁定、**滑动窗口限流**（验证码下发配额）与**定时任务执行锁**跨实例
  共享（未配置时为进程内存实现，仅单实例有效——多实例下验证码配额会被放大 N 倍，
  定时任务会在每个实例各跑一遍）；
  WebSocket 预警广播在多实例下需接入 Redis Pub/Sub 等集中消息总线；
- WebSocket 客户端建议使用首帧鉴权（连接后第一条文本帧发送 JWT），
  避免令牌经 URL query 进入访问日志；
- 居民端必须配置真实短信/微信通道（`MEDPLAT_SMS_PROVIDER=http`、
  `MEDPLAT_WECHAT_PROVIDER=official`），并设 `MEDPLAT_PORTAL_LEGACY_VERIFY=false`
  关闭旧的"健康卡号+身份证号"免登录查询接口。
