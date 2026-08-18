# AGENTS.md

本项目的代理约定以 [`CLAUDE.md`](./CLAUDE.md) 为准——请先阅读它，再动代码。

## 首要一条

**新建任何东西之前，先搜仓库是否已有等价实现**（工具、组件、服务、类型、接口、迁移、规则）。
本仓库已因重复实现积累了严重的三套并行子域债务（见 `docs/TECH_DEBT.md`）——复用优先于新建。

## 每日工作流

按 `docs/日常开发工作流.md` 循环：**晨间**（git pull / CI / 昨天 PR）→ **定向阅读**（`ROADMAP.md`、`ARCHITECTURE.md`、本文件→`CLAUDE.md`、相关 ADR）→ 出计划、**先不写码** → **PLAN → 审批方向 → 实现 → tests → /review → PR → merge**。先计划后编码；方向未审批不动手；merge 由人决定。
