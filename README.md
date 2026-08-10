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
| 集中审方 | "系统+药师"双重审方、用药规则库 | `/api/prescriptions` |
| 中心药房 | 库存、余缺调拨、缺药预警 | `/api/pharmacy` |
| 慢病管理 | 建档、随访智能分级、超期预警、指导要点嵌入 | `/api/chronic` |
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
| 医保协同 | 结算、转诊证明、特病申报、基金监测 | `/api/insurance` |
| 远程医学教育 | 课程、培训考核、通过率统计 | `/api/education` |
| 老年健康 | ADL自动分级、失能清单 | `/api/eldercare` |
| 妇幼保健 | 孕产妇建册/高危/访视、儿童保健 | `/api/maternal` |
| 疫苗接种 | 接种登记、禁忌拦截、接种前评估 | `/api/vaccination` |
| 公卫协同 | 应急事件指挥、诊间医防提醒、五域卫生监测 | `/api/publichealth` |
| 综合管理 | 人力/派驻、财务集中核算、物资、公文、排班质控 | `/api/mgmt` |
| 对接适配层 | HL7 v2 ADT / FHIR R4 Patient·Observation 入站转换、FHIR 导出 | `/api/integration` |
| 实时消息 | WebSocket 危急值/缺药预警秒级广播、角色化任务待办中心 | `/ws/notifications`、`/api/todos` |
| 居民端移动版 | H5 移动优先：健康宣教、双因子查档案、满意度评价 | `/m` |
| 智能化 | 相互作用审方、慢病风险评分、药品采购建议 | `/api/prescriptions`、`/api/chronic/{id}/risk`、`/api/pharmacy/purchase-suggestions` |

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

## 生产部署

```bash
docker compose up -d                          # 一键起 app + PostgreSQL 16（命名卷持久化）
cd server && alembic upgrade head             # 结构迁移（与应用共用 MEDPLAT_DATABASE_URL）
sh scripts/backup.sh /data/backups            # pg_dump 备份（建议 crontab 定时）
```
