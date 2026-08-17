"""全域慢专病全流程管理系统：50 张 spd_* 表

Revision ID: d1a2b3c4e5f6
Revises: c2d3e4f5a6b7

按招标文件《服务内容及要求》十一端建的领域表，分八组：
配置（病种/目标/路径/量表/宣教/服务包/标签/中心）、组织（团队/村医/设备/数据源）、
人群（筛查/目标池/纳管/生命周期/召回/分组/服务包绑定）、服务（路径实例/统一任务/
监测/评估/干预/宣教推送/复诊/上报/健康处方/咨询/服务申请）、转诊（规则/单据/轨迹）、
考核（指标/方案/结果/积分/商品/兑换/签到）、智能随访（方案/问卷/记录/呼叫/抽查）、
智能辅助（报告模板/推送任务/报告实例）。

建表顺序按外键依赖拓扑排序：被引用的表先建，否则 PostgreSQL 会在建表时报
"relation does not exist"（SQLite 不校验，本地跑得过、上生产才炸）。
"""
import sqlalchemy as sa
from alembic import op

revision = "d1a2b3c4e5f6"
down_revision = "c2d3e4f5a6b7"
# 子系统自成一条迁移分支：慢专病与主平台并行开发时不再抢同一个 head，
# 各自往自己的链上追加即可。代价是升级命令要用 `alembic upgrade heads`（复数），
# 已在 docs/运维手册.md 与 start.sh 里统一。
branch_labels = ("spd",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spd_assess_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False, index=True),
        sa.Column("program_codes", sa.JSON(), nullable=False),
        sa.Column("object_type", sa.String(16), nullable=False),
        sa.Column("period_type", sa.String(16), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_scales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("scoring", sa.JSON(), nullable=False),
        sa.Column("qr_token", sa.String(32), nullable=False, index=True),
        sa.Column("owner_team_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("code", "version", name="uq_spd_scale_code_version"),
    )
    op.create_table(
        "spd_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("scale_id", sa.Integer(), sa.ForeignKey("spd_scales.id"), nullable=False, index=True),
        sa.Column("scale_code", sa.String(32), nullable=False, index=True),
        sa.Column("scale_version", sa.String(16), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False, index=True),
        sa.Column("advice", sa.String(512), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_call_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("phone", sa.String(20), nullable=False, index=True),
        sa.Column("ref_type", sa.String(24), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False),
        sa.Column("record_url", sa.String(256), nullable=False),
        sa.Column("result", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("level", sa.String(16), nullable=False, index=True),
        sa.Column("program_codes", sa.JSON(), nullable=False),
        sa.Column("leader_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("service_area", sa.String(256), nullable=False),
        sa.Column("data_scope", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("screening_id", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("spd_teams.id"), nullable=True),
        sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("matched_rules", sa.JSON(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.UniqueConstraint("patient_id", "program_code", name="uq_spd_candidate_patient_program"),
    )
    op.create_index("ix_spd_candidate_status_org", "spd_candidates", ["status", "org_id"])
    op.create_table(
        "spd_case_report_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("manager_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("assignee_ids", sa.JSON(), nullable=False),
        sa.Column("org_ids", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_case_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("spd_case_report_tasks.id"), nullable=True, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("report_type", sa.String(16), nullable=False, index=True),
        sa.Column("content", sa.String(1024), nullable=False),
        sa.Column("trigger_rule", sa.String(128), nullable=False),
        sa.Column("reporter_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("handled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("handle_note", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "spd_centers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("lead_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("lead_dept", sa.String(64), nullable=False),
        sa.Column("leader_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("org_ids", sa.JSON(), nullable=False),
        sa.Column("team_ids", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_consults",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "spd_consult_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("consult_id", sa.Integer(), sa.ForeignKey("spd_consults.id"), nullable=False, index=True),
        sa.Column("sender", sa.String(16), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_data_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("endpoint", sa.String(256), nullable=False),
        sa.Column("freq_minutes", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_rows", sa.Integer(), nullable=False),
        sa.Column("last_latency_ms", sa.Integer(), nullable=False),
        sa.Column("success_rate", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
    )
    op.create_table(
        "spd_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sn", sa.String(64), nullable=False, index=True),
        sa.Column("device_type", sa.String(16), nullable=False, index=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("bound_patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=True, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_edu_materials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("content", sa.String(8192), nullable=False),
        sa.Column("media_url", sa.String(256), nullable=False),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_edu_pushes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_id", sa.Integer(), sa.ForeignKey("spd_edu_materials.id"), nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("send_at", sa.String(19), nullable=False),
        sa.Column("frequency", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("spd_teams.id"), nullable=True, index=True),
        sa.Column("doctor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("manager_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("village_doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("stage", sa.String(32), nullable=False, index=True),
        sa.Column("risk_level", sa.String(16), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("sign_date", sa.String(10), nullable=False),
        sa.Column("consent_signed", sa.Boolean(), nullable=False),
        sa.Column("consent_no", sa.String(64), nullable=False),
        sa.Column("service_start", sa.String(10), nullable=False),
        sa.Column("service_end", sa.String(10), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, index=True),
        sa.Column("habits", sa.JSON(), nullable=False),
        sa.Column("risk_factors", sa.JSON(), nullable=False),
        sa.Column("complications", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("last_followup_at", sa.String(10), nullable=False),
        sa.Column("next_followup_at", sa.String(10), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.UniqueConstraint("patient_id", "program_code", name="uq_spd_enroll_patient_program"),
    )
    op.create_index("ix_spd_enroll_org_status", "spd_enrollments", ["org_id", "status"])
    op.create_table(
        "spd_followup_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("scene", sa.String(16), nullable=False, index=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("diagnosis_keywords", sa.JSON(), nullable=False),
        sa.Column("surgery_keywords", sa.JSON(), nullable=False),
        sa.Column("order_keywords", sa.JSON(), nullable=False),
        sa.Column("points", sa.JSON(), nullable=False),
        sa.Column("questionnaire_code", sa.String(32), nullable=False),
        sa.Column("executor_role", sa.String(32), nullable=False),
        sa.Column("allow_depts", sa.JSON(), nullable=False),
        sa.Column("allow_roles", sa.JSON(), nullable=False),
        sa.Column("preset", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
    )
    op.create_table(
        "spd_followup_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("spd_followup_rules.id"), nullable=True),
        sa.Column("questionnaire_code", sa.String(32), nullable=False),
        sa.Column("scene", sa.String(16), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("planned_at", sa.String(10), nullable=False, index=True),
        sa.Column("executed_at", sa.String(10), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("executor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("abnormal_level", sa.String(16), nullable=False, index=True),
        sa.Column("result", sa.String(512), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_index("ix_spd_fu_status_planned", "spd_followup_records", ["status", "planned_at"])
    op.create_table(
        "spd_goods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.String(256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
    )
    op.create_table(
        "spd_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, index=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, index=True),
        sa.Column("auto_rule", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_group_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("spd_groups.id"), nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("group_id", "patient_id", name="uq_spd_group_member"),
    )
    op.create_table(
        "spd_health_prescriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("drug_advice", sa.String(1024), nullable=False),
        sa.Column("rehab_advice", sa.String(1024), nullable=False),
        sa.Column("life_advice", sa.String(1024), nullable=False),
        sa.Column("target_note", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_indicators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_codes", sa.JSON(), nullable=False),
        sa.Column("object_type", sa.String(16), nullable=False, index=True),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("scope_expr", sa.String(256), nullable=False),
        sa.Column("formula", sa.String(256), nullable=False),
        sa.Column("score_rule", sa.JSON(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("abnormal_rule", sa.String(256), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("effective_from", sa.String(10), nullable=False),
        sa.Column("effective_scope", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("code", "version", name="uq_spd_indicator_code_version"),
    )
    op.create_table(
        "spd_intervention_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("content", sa.String(2048), nullable=False),
        sa.Column("measures", sa.String(1024), nullable=False),
        sa.Column("frequency", sa.String(32), nullable=False),
        sa.Column("cycle_days", sa.Integer(), nullable=False),
        sa.Column("auto_risk_level", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "spd_interventions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=True, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("spd_intervention_templates.id"), nullable=True),
        sa.Column("goal", sa.String(256), nullable=False),
        sa.Column("content", sa.String(2048), nullable=False),
        sa.Column("measures", sa.String(1024), nullable=False),
        sa.Column("frequency", sa.String(32), nullable=False),
        sa.Column("next_at", sa.String(10), nullable=False, index=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("feedback", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=False, index=True),
        sa.Column("event", sa.String(16), nullable=False, index=True),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("detail", sa.String(512), nullable=False),
        sa.Column("target_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("occurred_at", sa.String(10), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_measurements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("metric", sa.String(32), nullable=False, index=True),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("level", sa.String(16), nullable=False, index=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("device_sn", sa.String(64), nullable=False),
        sa.Column("measured_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("note", sa.String(256), nullable=False),
    )
    op.create_index("ix_spd_meas_patient_metric", "spd_measurements", ["patient_id", "metric", "measured_at"])
    op.create_table(
        "spd_service_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "spd_package_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=False, index=True),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("spd_service_packages.id"), nullable=False, index=True),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("bound_at", sa.DateTime(), nullable=False),
        sa.Column("unbound_at", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.String(10), nullable=False),
    )
    op.create_table(
        "spd_package_usages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("spd_package_bindings.id"), nullable=False, index=True),
        sa.Column("item_code", sa.String(32), nullable=False),
        sa.Column("item_name", sa.String(64), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("note", sa.String(256), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, index=True),
        sa.Column("lead_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("lead_dept", sa.String(64), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("include_rules", sa.JSON(), nullable=False),
        sa.Column("exclude_rules", sa.JSON(), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("milestones", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("effective_from", sa.String(10), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_path_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("spd_programs.id"), nullable=False, index=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("scene", sa.String(16), nullable=False, index=True),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("copied_from_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("code", "version", name="uq_spd_path_code_version"),
    )
    op.create_table(
        "spd_path_instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=False, index=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("spd_path_templates.id"), nullable=False, index=True),
        sa.Column("template_code", sa.String(32), nullable=False),
        sa.Column("current_node_key", sa.String(32), nullable=False, index=True),
        sa.Column("current_stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_table(
        "spd_path_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("spd_path_templates.id"), nullable=False, index=True),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("exec_role", sa.String(32), nullable=False),
        sa.Column("service_type", sa.String(16), nullable=False),
        sa.Column("enter_condition", sa.JSON(), nullable=False),
        sa.Column("complete_condition", sa.JSON(), nullable=False),
        sa.Column("next_key", sa.String(32), nullable=False),
        sa.Column("due_days", sa.Integer(), nullable=False),
        sa.Column("timeout_action", sa.String(16), nullable=False),
        sa.Column("require_form", sa.Boolean(), nullable=False),
        sa.Column("require_evidence", sa.Boolean(), nullable=False),
        sa.Column("form_code", sa.String(32), nullable=False),
        sa.Column("note", sa.String(256), nullable=False),
        sa.UniqueConstraint("template_id", "key", name="uq_spd_node_key"),
    )
    op.create_table(
        "spd_point_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.Column("earned", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_point_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("spd_point_accounts.id"), nullable=False, index=True),
        sa.Column("rule_code", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, index=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("ref_type", sa.String(24), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_point_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("event", sa.String(24), nullable=False, index=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("condition", sa.String(256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
    )
    op.create_table(
        "spd_program_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("spd_programs.id"), nullable=False, index=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=False),
        sa.Column("note", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_qc_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("spd_followup_records.id"), nullable=False, index=True),
        sa.Column("batch", sa.String(32), nullable=False, index=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("sampler_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("result", sa.String(16), nullable=False, index=True),
        sa.Column("note", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_questionnaires",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("scene", sa.String(16), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("abnormal_rules", sa.JSON(), nullable=False),
        sa.Column("track_dept", sa.String(64), nullable=False),
        sa.Column("handle_role", sa.String(32), nullable=False),
        sa.Column("preset", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
    )
    op.create_table(
        "spd_recalls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=False, index=True),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("contacts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("result", sa.String(256), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "spd_redeems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("spd_point_accounts.id"), nullable=False, index=True),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("spd_goods.id"), nullable=False, index=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("verify_code", sa.String(16), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("verified_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "spd_referral_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=True, index=True),
        sa.Column("direction", sa.String(8), nullable=False, index=True),
        sa.Column("initiator_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("initiator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("current_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("current_level", sa.String(16), nullable=False, index=True),
        sa.Column("target_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, index=True),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("trigger_rule_code", sa.String(32), nullable=False),
        sa.Column("trigger_evidence", sa.JSON(), nullable=False),
        sa.Column("materials", sa.JSON(), nullable=False),
        sa.Column("effective_visit", sa.Boolean(), nullable=False, index=True),
        sa.Column("stable_for_down", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_spd_ref_status_org", "spd_referral_cases", ["status", "current_org_id"])
    op.create_table(
        "spd_referral_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("scene", sa.String(16), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("notify_role", sa.String(32), nullable=False),
        sa.Column("handle_level", sa.String(16), nullable=False),
        sa.Column("target_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("auto_task", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
    )
    op.create_table(
        "spd_referral_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("spd_referral_cases.id"), nullable=False, index=True),
        sa.Column("step", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("opinion", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_report_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("period", sa.String(16), nullable=False, index=True),
        sa.Column("scope_level", sa.String(16), nullable=False, index=True),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_report_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("spd_report_templates.id"), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("push_time", sa.String(5), nullable=False),
        sa.Column("subscriber_ids", sa.JSON(), nullable=False),
        sa.Column("org_ids", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.String(10), nullable=False),
        sa.Column("valid_to", sa.String(10), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_report_instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("spd_report_tasks.id"), nullable=True),
        sa.Column("template_code", sa.String(32), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("period_label", sa.String(32), nullable=False, index=True),
        sa.Column("scope_level", sa.String(16), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("subscriber_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_revisits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("plan_date", sa.String(10), nullable=False, index=True),
        sa.Column("dept", sa.String(64), nullable=False),
        sa.Column("doctor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("items", sa.String(512), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("remind_status", sa.String(16), nullable=False),
        sa.Column("actual_date", sa.String(10), nullable=False),
        sa.Column("log", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spd_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("spd_assess_plans.id"), nullable=False, index=True),
        sa.Column("period", sa.String(16), nullable=False, index=True),
        sa.Column("object_type", sa.String(16), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False, index=True),
        sa.Column("object_name", sa.String(64), nullable=False),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.UniqueConstraint("plan_id", "period", "object_type", "object_id", name="uq_spd_score"),
    )
    op.create_table(
        "spd_screenings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("source", sa.String(16), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("scale_code", sa.String(32), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False, index=True),
        sa.Column("result", sa.String(16), nullable=False, index=True),
        sa.Column("advice", sa.String(512), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False, index=True),
        sa.Column("review_result", sa.String(16), nullable=False),
        sa.Column("review_note", sa.String(256), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "spd_service_applies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("screening_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("handled_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("handle_note", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "spd_signins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("spd_point_accounts.id"), nullable=False, index=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "day", name="uq_spd_signin_day"),
    )
    op.create_table(
        "spd_sync_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("spd_data_sources.id"), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("message", sa.String(256), nullable=False),
    )
    op.create_table(
        "spd_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("color", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "spd_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("spd_programs.id"), nullable=False, index=True),
        sa.Column("stage", sa.String(32), nullable=False, index=True),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_low", sa.Float(), nullable=True),
        sa.Column("target_high", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("qualitative", sa.String(128), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("followup_interval_days", sa.Integer(), nullable=False),
        sa.Column("form_code", sa.String(32), nullable=False),
        sa.Column("edu_code", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("program_id", "stage", "metric", name="uq_spd_target_stage_metric"),
    )
    op.create_table(
        "spd_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_code", sa.String(32), nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("spd_enrollments.id"), nullable=True, index=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("spd_path_instances.id"), nullable=True, index=True),
        sa.Column("node_key", sa.String(32), nullable=False),
        sa.Column("task_type", sa.String(16), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("spd_teams.id"), nullable=True),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("exec_role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("priority", sa.Integer(), nullable=False, index=True),
        sa.Column("due_date", sa.String(10), nullable=False, index=True),
        sa.Column("form_code", sa.String(32), nullable=False),
        sa.Column("require_evidence", sa.Boolean(), nullable=False),
        sa.Column("form", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("urged_count", sa.Integer(), nullable=False),
        sa.Column("escalated", sa.Boolean(), nullable=False, index=True),
        sa.Column("transferred_from", sa.Integer(), nullable=True),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("review_note", sa.String(256), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_spd_task_org_due", "spd_tasks", ["org_id", "due_date"])
    op.create_index("ix_spd_task_assignee_status", "spd_tasks", ["assignee_id", "status"])
    op.create_table(
        "spd_team_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("spd_teams.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("member_role", sa.String(16), nullable=False, index=True),
        sa.Column("program_codes", sa.JSON(), nullable=False),
        sa.Column("stage_scope", sa.String(64), nullable=False),
        sa.Column("patient_scope", sa.String(16), nullable=False),
        sa.Column("can_view", sa.Boolean(), nullable=False),
        sa.Column("can_followup", sa.Boolean(), nullable=False),
        sa.Column("can_referral", sa.Boolean(), nullable=False),
        sa.Column("can_audit", sa.Boolean(), nullable=False),
        sa.Column("can_assess", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("team_id", "user_id", name="uq_spd_team_member"),
    )
    op.create_table(
        "spd_village_doctors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("township", sa.String(64), nullable=False, index=True),
        sa.Column("village", sa.String(64), nullable=False, index=True),
        sa.Column("license_no", sa.String(64), nullable=False),
        sa.Column("license_valid_to", sa.String(10), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("bind_token", sa.String(32), nullable=False, index=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )



def downgrade() -> None:
    # 逆序删表：先删引用方，再删被引用方
    op.drop_table("spd_village_doctors")
    op.drop_table("spd_team_members")
    op.drop_table("spd_tasks")
    op.drop_table("spd_targets")
    op.drop_table("spd_tags")
    op.drop_table("spd_sync_logs")
    op.drop_table("spd_signins")
    op.drop_table("spd_service_applies")
    op.drop_table("spd_screenings")
    op.drop_table("spd_scores")
    op.drop_table("spd_revisits")
    op.drop_table("spd_report_instances")
    op.drop_table("spd_report_tasks")
    op.drop_table("spd_report_templates")
    op.drop_table("spd_referral_steps")
    op.drop_table("spd_referral_rules")
    op.drop_table("spd_referral_cases")
    op.drop_table("spd_redeems")
    op.drop_table("spd_recalls")
    op.drop_table("spd_questionnaires")
    op.drop_table("spd_qc_samples")
    op.drop_table("spd_program_versions")
    op.drop_table("spd_point_rules")
    op.drop_table("spd_point_records")
    op.drop_table("spd_point_accounts")
    op.drop_table("spd_path_nodes")
    op.drop_table("spd_path_instances")
    op.drop_table("spd_path_templates")
    op.drop_table("spd_programs")
    op.drop_table("spd_package_usages")
    op.drop_table("spd_package_bindings")
    op.drop_table("spd_service_packages")
    op.drop_table("spd_measurements")
    op.drop_table("spd_lifecycle_events")
    op.drop_table("spd_interventions")
    op.drop_table("spd_intervention_templates")
    op.drop_table("spd_indicators")
    op.drop_table("spd_health_prescriptions")
    op.drop_table("spd_group_members")
    op.drop_table("spd_groups")
    op.drop_table("spd_goods")
    op.drop_table("spd_followup_records")
    op.drop_table("spd_followup_rules")
    op.drop_table("spd_enrollments")
    op.drop_table("spd_edu_pushes")
    op.drop_table("spd_edu_materials")
    op.drop_table("spd_devices")
    op.drop_table("spd_data_sources")
    op.drop_table("spd_consult_messages")
    op.drop_table("spd_consults")
    op.drop_table("spd_centers")
    op.drop_table("spd_case_reports")
    op.drop_table("spd_case_report_tasks")
    op.drop_table("spd_candidates")
    op.drop_table("spd_teams")
    op.drop_table("spd_call_tasks")
    op.drop_table("spd_assessments")
    op.drop_table("spd_scales")
    op.drop_table("spd_assess_plans")
