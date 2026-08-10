# 县域医共体信息化平台（medplat）

紧密型县域医共体信息化平台，依据国家卫生健康委《紧密型县域医共体信息化功能指引》（国卫办规划函〔2025〕63号）等文件建设。

- 建设规划：[县域医共体信息化平台建设规划.md](县域医共体信息化平台建设规划.md)
- 开发时间计划：[docs/开发时间计划.md](docs/开发时间计划.md)

## 当前进度（第一期 · M0 平台骨架）

后端基础平台已就绪，包含：

| 模块 | 说明 | 接口前缀 |
|---|---|---|
| 统一认证 | 登录、JWT 令牌、角色权限 | `/api/auth` |
| 机构管理 | 县-乡-村三级医共体成员单位 | `/api/organizations` |
| 患者主索引 | EMPI，电子健康卡号，幂等建档 | `/api/patients` |
| 统一编码字典 | 诊断/药品/耗材/收费"四统一" | `/api/dictionaries` |
| 双向转诊 | 上转/下转申请与状态流转 | `/api/referrals` |

## 快速开始

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload      # 启动开发服务器，接口文档见 http://127.0.0.1:8000/docs
python -m pytest tests/ -q          # 运行测试
```

初始管理员账号：`admin` / `admin123`（生产部署前必须修改，并通过 `MEDPLAT_SECRET` 环境变量设置令牌密钥）。

数据库默认使用 SQLite（开发环境），通过 `MEDPLAT_DATABASE_URL` 环境变量可切换 PostgreSQL。
