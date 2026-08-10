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
| 绩效考核 | 按机构自动汇算五维评分排名 | `/api/performance` |

演示数据：启动服务后执行 `python scripts/seed_demo.py` 一键灌入。

## 快速开始

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload      # 启动开发服务器，接口文档见 http://127.0.0.1:8000/docs
python -m pytest tests/ -q          # 运行测试
```

初始管理员账号：`admin` / `admin123`（生产部署前必须修改，并通过 `MEDPLAT_SECRET` 环境变量设置令牌密钥）。

数据库默认使用 SQLite（开发环境），通过 `MEDPLAT_DATABASE_URL` 环境变量可切换 PostgreSQL。
