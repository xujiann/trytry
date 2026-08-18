# DATA_MODEL.md — 数据模型地图

> 246 张表（平台 187 + 慢专病 59）、迁移拓扑、类型约定、种子机制。仅描述现状（AS-IS）。
> 来源：`server/app/models.py`（3950 行）、`server/app/spd/models.py`（1398 行）、`server/alembic/versions/`（52 个）。

---

## 1. 总量与约定

| 指标 | 平台 (models.py) | 慢专病 (spd/models.py) | 合计 |
|---|---:|---:|---:|
| 表数（`__tablename__`） | 187 | 59 | **246** |
| 主键 | 全部 `id: Integer, primary_key=True` | 同 | 自增代理键，零例外 |
| 外键（`ForeignKey`） | 299（1.59/表） | 113（1.90/表） | 76 处 spd→平台 |
| `created_at` 覆盖 | 缺 33 张 | 缺 19 张 | **缺 52 张** |
| `updated_at` 覆盖 | 8 | 2 | **10/246（4%）** |
| 软删除 | 无 | 无 | 全物理删除/status 字段 |
| `relationship()` | 31 | **0** | ORM 关系基本不用，无 cascade |
| JSON 列 | 13 | 60（30/59 表） | spd 侧密度极高 |
| 复合索引（`Index`） | **2** | 7 | 平台侧近乎缺席 |

**约定命名前缀**：`uq_`（唯一约束）、`ix_`（索引）。**跨表引用**：spd 侧另有 30 处 `program_code` 等字符串软外键（DB 层无约束）。

## 2. 三套并行子域（同一概念、三处建表）——最重要的数据现状

| 概念 | 公卫慢病 (chronic) | 院内专病 (disease) | 全域慢专病 (spd) |
|---|---|---|---|
| 病种目录 | `chronic_disease_types` | `disease_programs` | `spd_programs` |
| 患者入组 | `chronic_patients` | `disease_enrollments` | `spd_enrollments`(+`spd_candidates`) |
| 路径/随访 | `followups` | `disease_path_records` | `spd_path_instances`/`spd_followup_records`/`spd_revisits` |
| 分级算法 | `_metric_level`+`level_rules` JSON | 路径节点 | `judge_level`+`SpdTarget` |
| 病种种子 code | `hypertension`/`diabetes`/`copd` | — | **相同的** `hypertension`/`diabetes` |

> **直接后果**：同一"高血压"病种在库里有两份互不感知的定义与阈值（`chronic_seed.py:26` vs `spd/seed.py:19`），统计口径必然对不上，且无任何约束能发现偏离。详见 `TECH_DEBT.md`。

**其它跨侧重复/相似表**：`followup_tasks` ↔ `spd_tasks`；`referrals`+`referral_certs` ↔ `spd_referral_cases`+`_steps`+`_rules`；`consultations`/`online_consults` ↔ `spd_consults`；`performance_indicators` ↔ `spd_indicators`；`report_templates`/`print_templates` ↔ `spd_report_templates`；`health_monitor_records` ↔ `spd_measurements`；`elderly_assessments` ↔ `spd_assessments`。

**共享底座**：只有 `patients` / `organizations` / `users` 三张主数据表被 spd 通过外键直接引用（76 处）。

## 3. 平台侧表按业务域分组（187 张）

| 域 | 表数 | 代表表 |
|---|---:|---|
| A 平台底座/权限/审计 | 12 | users, roles, permissions, role_permissions, audit_logs, access_logs, organizations, org_groups, departments, system_params |
| B 主数据与字典 | 6 | patients, code_systems, code_entries, drg_groups, knowledge_entries |
| C 门急诊与病历文书 | 10 | encounters, medical_records, progress_notes, medical_certs, informed_consents, visit_credentials |
| D 检查检验/病理/报告 | 7 | exam_requests, exam_reports, critical_actions, report_templates, pathology_specimens |
| E 药事与药品供应链 | 13 | drug_rules, prescriptions, prescription_items, drug_stocks, stock_transfers, suppliers, purchase_orders |
| F 中医药 | 4 | tcm_formulas, tcm_preparation_batches, tcm_techniques, tcm_master_cases |
| G 住院/护理/输血/消毒供应 | 12 | wards, beds, admissions, nursing_records, vital_sign_records, blood_stocks, cssd_requests |
| H 手术麻醉 | 4 | operating_rooms, surgery_requests, surgery_schedules, surgery_records |
| I 收费结算与医保基金 | 13 | charge_items, bill_details, settlements, payment_orders, fund_pools, fund_settlements, fund_distributions |
| J 财务会计/成本/资产/物资 | 11 | account_subjects, vouchers, voucher_entries, budgets, department_costs, assets, material_purchases |
| K 人事与绩效 | 9 | employees, staff_contracts, secondments, duty_rosters, performance_indicators, performance_formulas |
| L 慢病/专病/家医签约 | 11 | chronic_disease_types, chronic_patients, followups, disease_programs, fd_contracts, home_visit_orders |
| M 妇幼儿童与老年 | 9 | maternal_records, child_records, delivery_records, newborn_screenings, elderly_assessments |
| N 免疫规划与疾控 | 11 | vaccination_records, vaccine_batches, cold_chain_records, infectious_cases, syndrome_monitors, ph_events |
| O 急救应急与医废 | 6 | emergency_cases, emergency_vitals, medical_wastes, waste_locations |
| P 医疗质量与安全 | 7 | qc_records, qc_rules, record_qcs, adverse_events, infection_reports |
| Q 转诊/远程/预约/资源 | 11 | referrals, consultations, appointments, resources, archive_authorizations, satisfaction_surveys |
| R 教育培训与宣教 | 9 | courses, training_records, training_assessments, health_articles, live_sessions |
| S 集成/规则流程/调度 | 11 | esb_endpoints, esb_messages, esb_flows, rule_definitions, workflow_definitions, scheduled_jobs, job_runs |
| T 居民端账号 | 3 | resident_accounts, resident_family_members, sms_codes |
| U 通用能力与行政 | 8 | attachments, notifications, print_templates, official_docs, admin_projects, simulation_cases |

## 4. spd 侧表按域分组（59 张）

| 域 | 表数 | 起始行 |
|---|---:|---|
| 配置域（病种/目标/路径/量表/宣教/服务包/中心） | 10 | `spd/models.py:50` |
| 组织域（团队/村医/设备/数据源/同步） | 6 | `:297` |
| 人群域（筛查/目标池/纳管/生命周期/分组） | 9 | `:432` |
| 服务域（路径实例/任务/随访/干预/评估/监测/上报/咨询） | 14 | `:653` |
| 转诊域（规则/病例/步骤） | 3 | `:973` |
| 考核域（指标/方案/计分/积分/商品/兑换/签到） | 9 | `:1054` |
| 智能随访域（规则/问卷/记录/外呼/质检） | 5 | `:1225` |
| 智能辅助域（报告模板/任务/实例） | 3 | `:1340` |

## 5. 类型约定

| 类型 | 现状 |
|---|---|
| **金额** | ✅ `Money = Numeric(14,2, asdecimal=False)`，48 列/28 表。已从 Float 迁移，配套方言测试。剩余 Float 均无金额语义（临床值/权重），仅 `fund_distributions`/`cost_allocation_rules` 浮点占比参与金额推导需注意 |
| **日期** | `String(10)`（79 列），`datetypes.py` 仅做 Pydantic 入参校验，**DB 层无 DATE 约束**；同库另有 `DateTime` 时间戳，两种存储策略并存 |
| **时间戳** | naive UTC（`utcnow()` → `clock.now_naive()`），记录了 aware/naive 混用事故 |
| **长文本** | **无 Text 类型**，一律 `String(N)`，最长 `String(1024)`——病程/知情同意书正文有截断风险（PG 硬约束） |
| **枚举** | **无 Enum**，状态一律裸字符串，DB 层零约束（国产库兼容 + 口径易变） |
| **JSON** | spd 侧 60 列，多对多关系被拍扁进 JSON 数组（`org_ids`/`assignee_ids`），无法索引与 join |
| **PII** | `patients.id_card`/`phone` **明文存储 + 建索引**，仅出口脱敏；`gmcrypto` 在模型层零引用，无静态加密 |

## 6. 迁移拓扑

```
c1627502ad43 (初始基线, root)
      │ ... 线性 ...
      ▼
c2d3e4f5a6b7 ─┬─▶ d1a2b3c4e5f6 (全域慢专病) branch_labels=("spd",)
              │        ▼ ... ▼  d2b3c4d5e6f7  ← HEAD 1 (spd 链)
              └─▶ e0f1a2b3c4d5 ... ▼  d3e4f5a6b7c8  ← HEAD 2 (平台链)
```

| 指标 | 值 |
|---|---|
| 迁移文件数 | 52 |
| 根节点 | 1 |
| merge 修订 | **0** |
| **HEAD 数** | **2**（平台 + spd，故须 `alembic upgrade heads` 复数） |
| 空 `downgrade()` | **0/52**（全部实现回滚 ✅） |
| 模型有迁移没建的表 | **0**（零漂移 ✅） |
| 迁移建过模型没有的表 | 1（`appointment_blacklist` 已合并，非孤儿） |

**问题**：README 写 `upgrade head`（单数）在双 head 下必然失败且漏 spd 59 表；revision id 手写伪 hex，存在 3 组仅差 1 位的近碰撞；文件名 51/52 中文 slug，混杂 5 套编号体系；`alembic.ini` 未启用时间戳前缀，字母序与拓扑无关。

## 7. create_all 与 alembic 双轨

| 位置 | 用途 |
|---|---|
| `main.py:113` | **lifespan 每次启动执行 create_all**（生产路径） |
| `conftest.py:16` | 测试建库 |
| `alembic/env.py:23` | `target_metadata = Base.metadata` |

风险：create_all 只建不存在的表不改列。模型加列而迁移没写时，SQLite 开发库悄悄建对、生产 PG 表已存在什么也不做 → 上线才炸。该坑已真实发生（`补迁移九表` 迁移的注释即记录）。**当前漂移检查结果：干净。**

## 8. 种子数据机制

- **何时执行**：lifespan 启动钩子，14 段，每次进程启动都跑。
- **幂等模式统一**：先查库内已有 code 成 set，`if code not in existing: db.add(...)`——"只增不改"（不覆盖现场调过的参数）。
- **代价**：改种子后已部署库拿不到更新（无版本号/updated_at 比对）；14 个独立 commit 非事务性；lifespan 无 rollback 兜底，一段脏数据全站起不来。

## 9. 引用完整性缺口

- **`patients` 表无任何外键**（无 org_id），患者归属只能靠 join 其它表推导（visibility 全靠这个）。
- 38 张平台表 + 12 张 spd 表无外键；`job_runs` 不指向 `scheduled_jobs`、`sms_codes` 不指向 `resident_accounts`、`charge_items`/`suppliers`/`blood_stocks` 不指向 `organizations`。
- spd 侧 30 处字符串软外键（`program_code`），删除引用目标无报错。
- 无 `relationship()`/cascade：删 `spd_programs` 不级联清理 `spd_targets`。

*本文件仅描述现状，未对任何代码进行修改。*
