# SCHEMA（自动生成，勿手改）

> 由 `server/scripts/dump_schema.py` 从 ORM 元数据生成。改了模型请重跑该脚本。
> 表总数：**246**。类型/关系/迁移的解读见 `docs/DATA_MODEL.md`。

## access_logs

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · → users.id
- `username` · VARCHAR(64) · NOT NULL · index
- `org_id` · INTEGER · → organizations.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `resource` · VARCHAR(32) · NOT NULL · index
- `basis` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_access_logs_basis(basis)
- _index_ ix_access_logs_created_at(created_at)
- _index_ ix_access_logs_patient_id(patient_id)
- _index_ ix_access_logs_resource(resource)
- _index_ ix_access_logs_username(username)

## account_subjects

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(16) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL · index
- `direction` · VARCHAR(8) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_account_subjects_active(active)
- _index_ ix_account_subjects_category(category)
- _index_ ix_account_subjects_code(code) UNIQUE

## admin_projects

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `name` · VARCHAR(256) · NOT NULL
- `category` · VARCHAR(32) · NOT NULL · index
- `owner_name` · VARCHAR(64) · NOT NULL
- `start_date` · VARCHAR(10) · NOT NULL
- `due_date` · VARCHAR(10) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `progress_pct` · INTEGER · NOT NULL
- `budget_amount` · NUMERIC(14, 2) · NOT NULL
- `description` · VARCHAR(1024) · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_admin_projects_category(category)
- _index_ ix_admin_projects_due_date(due_date)
- _index_ ix_admin_projects_org_id(org_id)
- _index_ ix_admin_projects_status(status)

## admissions

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `ward_id` · INTEGER · NOT NULL · → wards.id
- `bed_id` · INTEGER · NOT NULL · → beds.id
- `doctor_name` · VARCHAR(64) · NOT NULL
- `diagnosis_name` · VARCHAR(256) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `admitted_at` · DATETIME · NOT NULL
- `discharged_at` · DATETIME
- `created_by` · INTEGER · NOT NULL · → users.id
- _index_ ix_admissions_org_id(org_id)
- _index_ ix_admissions_patient_id(patient_id)
- _index_ ix_admissions_status(status)

## adverse_events

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `event_type` · VARCHAR(16) · NOT NULL · index
- `level` · VARCHAR(4) · NOT NULL
- `anonymous` · BOOLEAN · NOT NULL
- `reporter_name` · VARCHAR(64) · NOT NULL
- `description` · VARCHAR(2048) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `review_note` · VARCHAR(1024) · NOT NULL
- `reviewed_by` · VARCHAR(64) · NOT NULL
- `reviewed_at` · DATETIME
- `rectify_note` · VARCHAR(1024) · NOT NULL
- `rectified_by` · VARCHAR(64) · NOT NULL
- `rectified_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_adverse_events_event_type(event_type)
- _index_ ix_adverse_events_org_id(org_id)
- _index_ ix_adverse_events_status(status)

## aefi_reports

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `record_id` · INTEGER · index · → vaccination_records.id
- `vaccine_code` · VARCHAR(64) · NOT NULL · index
- `batch_no` · VARCHAR(64) · NOT NULL · index
- `reaction_type` · VARCHAR(16) · NOT NULL · index
- `symptom` · VARCHAR(512) · NOT NULL
- `onset_date` · VARCHAR(10) · NOT NULL · index
- `outcome` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `reported_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_aefi_reports_batch_no(batch_no)
- _index_ ix_aefi_reports_onset_date(onset_date)
- _index_ ix_aefi_reports_org_id(org_id)
- _index_ ix_aefi_reports_outcome(outcome)
- _index_ ix_aefi_reports_patient_id(patient_id)
- _index_ ix_aefi_reports_reaction_type(reaction_type)
- _index_ ix_aefi_reports_record_id(record_id)
- _index_ ix_aefi_reports_vaccine_code(vaccine_code)

## appointment_slots

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `resource_type` · VARCHAR(16) · NOT NULL
- `resource_name` · VARCHAR(128) · NOT NULL
- `employee_id` · INTEGER · index · → employees.id
- `slot_date` · VARCHAR(10) · NOT NULL · index
- `slot_time` · VARCHAR(16) · NOT NULL
- `capacity` · INTEGER · NOT NULL
- `booked` · INTEGER · NOT NULL
- _index_ ix_appointment_slots_employee_id(employee_id)
- _index_ ix_appointment_slots_org_id(org_id)
- _index_ ix_appointment_slots_slot_date(slot_date)

## appointments

- `id` · INTEGER · PK · NOT NULL
- `slot_id` · INTEGER · NOT NULL · index · → appointment_slots.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `status` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (slot_id, patient_id) uq_appointment_slot_patient
- _index_ ix_appointments_patient_id(patient_id)
- _index_ ix_appointments_slot_id(slot_id)

## archive_authorizations

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `grantee_org_id` · INTEGER · NOT NULL · index · → organizations.id
- `scope` · VARCHAR(16) · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_archive_authorizations_grantee_org_id(grantee_org_id)
- _index_ ix_archive_authorizations_patient_id(patient_id)
- _index_ ix_archive_authorizations_status(status)

## asset_movements

- `id` · INTEGER · PK · NOT NULL
- `asset_id` · INTEGER · NOT NULL · index · → assets.id
- `movement_type` · VARCHAR(16) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_asset_movements_asset_id(asset_id)

## assets

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `code` · VARCHAR(64) · NOT NULL
- `name` · VARCHAR(128) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (code)
- _index_ ix_assets_org_id(org_id)

## attachments

- `id` · INTEGER · PK · NOT NULL
- `filename` · VARCHAR(256) · NOT NULL
- `content_type` · VARCHAR(64) · NOT NULL
- `size` · INTEGER · NOT NULL
- `sha256` · VARCHAR(64) · NOT NULL · index
- `owner_type` · VARCHAR(32) · NOT NULL · index
- `owner_id` · INTEGER · NOT NULL · index
- `uploaded_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_attachments_owner_id(owner_id)
- _index_ ix_attachments_owner_type(owner_type)
- _index_ ix_attachments_sha256(sha256)

## audit_logs

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · → users.id
- `username` · VARCHAR(64) · NOT NULL · index
- `method` · VARCHAR(8) · NOT NULL
- `path` · VARCHAR(256) · NOT NULL · index
- `status_code` · INTEGER · NOT NULL
- `prev_hash` · VARCHAR(64) · NOT NULL
- `entry_hash` · VARCHAR(64) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_audit_logs_created_at(created_at)
- _index_ ix_audit_logs_entry_hash(entry_hash)
- _index_ ix_audit_logs_path(path)
- _index_ ix_audit_logs_username(username)

## beds

- `id` · INTEGER · PK · NOT NULL
- `ward_id` · INTEGER · NOT NULL · index · → wards.id
- `bed_no` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (ward_id, bed_no) uq_bed_ward_no
- _index_ ix_beds_status(status)
- _index_ ix_beds_ward_id(ward_id)

## bill_details

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `admission_id` · INTEGER · index · → admissions.id
- `encounter_id` · INTEGER · index · → encounters.id
- `item_code` · VARCHAR(64) · NOT NULL · index
- `item_name` · VARCHAR(128) · NOT NULL
- `unit_price` · NUMERIC(14, 2) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `amount` · NUMERIC(14, 2) · NOT NULL
- `settlement_id` · INTEGER · index · → settlements.id
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_bill_details_admission_id(admission_id)
- _index_ ix_bill_details_encounter_id(encounter_id)
- _index_ ix_bill_details_item_code(item_code)
- _index_ ix_bill_details_patient_id(patient_id)
- _index_ ix_bill_details_settlement_id(settlement_id)

## blood_stocks

- `id` · INTEGER · PK · NOT NULL
- `blood_type` · VARCHAR(4) · NOT NULL · index
- `component` · VARCHAR(16) · NOT NULL
- `quantity_ml` · INTEGER · NOT NULL
- _unique_ (blood_type, component) uq_blood_type_component
- _index_ ix_blood_stocks_blood_type(blood_type)

## budgets

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `year` · VARCHAR(4) · NOT NULL · index
- `category` · VARCHAR(8) · NOT NULL
- `amount` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (org_id, year, category) uq_budget_org_year_cat
- _index_ ix_budgets_org_id(org_id)
- _index_ ix_budgets_year(year)

## case_summaries

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · NOT NULL · index · → admissions.id
- `discharge_diagnosis` · VARCHAR(256) · NOT NULL
- `operation` · VARCHAR(256) · NOT NULL
- `total_cost` · NUMERIC(14, 2) · NOT NULL
- `drug_cost` · NUMERIC(14, 2) · NOT NULL
- `outcome` · VARCHAR(16) · NOT NULL
- `note` · VARCHAR(1024) · NOT NULL
- `drg_code` · VARCHAR(16) · NOT NULL · index
- `drg_weight` · FLOAT · NOT NULL
- `created_by_name` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_case_summaries_admission_id(admission_id) UNIQUE
- _index_ ix_case_summaries_drg_code(drg_code)

## charge_items

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL
- `price` · NUMERIC(14, 2) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_charge_items_code(code) UNIQUE

## charge_price_changes

- `id` · INTEGER · PK · NOT NULL
- `item_id` · INTEGER · NOT NULL · index · → charge_items.id
- `old_price` · NUMERIC(14, 2) · NOT NULL
- `new_price` · NUMERIC(14, 2) · NOT NULL
- `reason` · VARCHAR(256) · NOT NULL
- `effective_date` · VARCHAR(10) · NOT NULL
- `changed_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_charge_price_changes_created_at(created_at)
- _index_ ix_charge_price_changes_item_id(item_id)

## child_records

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL
- `gender` · VARCHAR(8) · NOT NULL
- `birth_date` · VARCHAR(10) · NOT NULL
- `guardian_patient_id` · INTEGER · → patients.id
- `created_at` · DATETIME · NOT NULL
- `high_risk` · BOOLEAN · NOT NULL · index
- `risk_note` · VARCHAR(256) · NOT NULL
- _index_ ix_child_records_high_risk(high_risk)

## child_visits

- `id` · INTEGER · PK · NOT NULL
- `child_id` · INTEGER · NOT NULL · index · → child_records.id
- `visit_type` · VARCHAR(16) · NOT NULL
- `height_cm` · FLOAT
- `weight_kg` · FLOAT
- `note` · VARCHAR(512) · NOT NULL
- `visit_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_child_visits_child_id(child_id)
- _index_ ix_child_visits_created_at(created_at)

## chronic_disease_types

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `level_rules` · JSON · NOT NULL
- `guidance` · VARCHAR(512) · NOT NULL
- `followup_interval_days` · INTEGER · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_chronic_disease_types_code(code) UNIQUE

## chronic_patients

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `disease` · VARCHAR(32) · NOT NULL · index
- `level` · INTEGER · NOT NULL · index
- `managed_by_org_id` · INTEGER · NOT NULL · → organizations.id
- `next_due` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (patient_id, disease) uq_chronic_patient_disease
- _index_ ix_chronic_patients_disease(disease)
- _index_ ix_chronic_patients_level(level)
- _index_ ix_chronic_patients_patient_id(patient_id)

## code_entries

- `id` · INTEGER · PK · NOT NULL
- `system_id` · INTEGER · NOT NULL · index · → code_systems.id
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(256) · NOT NULL · index
- _unique_ (system_id, code) uq_entry_system_code
- _index_ ix_code_entries_code(code)
- _index_ ix_code_entries_name(name)
- _index_ ix_code_entries_system_id(system_id)

## code_systems

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- _index_ ix_code_systems_code(code) UNIQUE

## cold_chain_records

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `device_name` · VARCHAR(128) · NOT NULL
- `temperature` · FLOAT · NOT NULL
- `min_allowed` · FLOAT · NOT NULL
- `max_allowed` · FLOAT · NOT NULL
- `exceeded` · BOOLEAN · NOT NULL · index
- `recorded_at` · VARCHAR(19) · NOT NULL · index
- `handled` · BOOLEAN · NOT NULL
- `handle_note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_cold_chain_records_exceeded(exceeded)
- _index_ ix_cold_chain_records_org_id(org_id)
- _index_ ix_cold_chain_records_recorded_at(recorded_at)

## consent_templates

- `id` · INTEGER · PK · NOT NULL
- `consent_type` · VARCHAR(16) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `body` · VARCHAR(8192) · NOT NULL
- `version` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_consent_templates_active(active)
- _index_ ix_consent_templates_consent_type(consent_type)

## consult_experts

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `specialty` · VARCHAR(64) · NOT NULL
- `available` · BOOLEAN · NOT NULL
- _unique_ (name)

## consultations

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `to_org_id` · INTEGER · NOT NULL · → organizations.id
- `question` · VARCHAR(1024) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `expert_name` · VARCHAR(64) · NOT NULL
- `opinion` · VARCHAR(2048) · NOT NULL
- `rating` · INTEGER · NOT NULL
- `fee` · NUMERIC(14, 2) · NOT NULL
- `fee_settled` · BOOLEAN · NOT NULL · index
- `fee_note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_consultations_fee_settled(fee_settled)
- _index_ ix_consultations_patient_id(patient_id)
- _index_ ix_consultations_status(status)

## cost_allocation_rules

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `from_dept_id` · INTEGER · NOT NULL · index · → departments.id
- `to_dept_id` · INTEGER · NOT NULL · index · → departments.id
- `ratio_pct` · FLOAT · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (from_dept_id, to_dept_id) uq_alloc_from_to
- _index_ ix_cost_allocation_rules_from_dept_id(from_dept_id)
- _index_ ix_cost_allocation_rules_org_id(org_id)
- _index_ ix_cost_allocation_rules_to_dept_id(to_dept_id)

## course_materials

- `id` · INTEGER · PK · NOT NULL
- `course_id` · INTEGER · NOT NULL · index · → courses.id
- `title` · VARCHAR(256) · NOT NULL
- `material_type` · VARCHAR(16) · NOT NULL · index
- `url` · VARCHAR(512) · NOT NULL
- `play_count` · INTEGER · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_course_materials_course_id(course_id)
- _index_ ix_course_materials_material_type(material_type)

## courses

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `course_type` · VARCHAR(8) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL
- `speaker` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL

## critical_actions

- `id` · INTEGER · PK · NOT NULL
- `report_id` · INTEGER · NOT NULL · index · → exam_reports.id
- `action` · VARCHAR(512) · NOT NULL
- `actor` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_critical_actions_report_id(report_id)

## cssd_cost_items

- `id` · INTEGER · PK · NOT NULL
- `batch_id` · INTEGER · NOT NULL · index · → sterilization_batches.id
- `cost_type` · VARCHAR(16) · NOT NULL · index
- `amount` · NUMERIC(14, 2) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_cssd_cost_items_batch_id(batch_id)
- _index_ ix_cssd_cost_items_cost_type(cost_type)

## cssd_requests

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `item_name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `batch_id` · INTEGER · → sterilization_batches.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_cssd_requests_org_id(org_id)
- _index_ ix_cssd_requests_status(status)

## delivery_records

- `id` · INTEGER · PK · NOT NULL
- `record_id` · INTEGER · NOT NULL · index · → maternal_records.id
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `delivery_date` · VARCHAR(10) · NOT NULL
- `delivery_mode` · VARCHAR(16) · NOT NULL
- `newborn_count` · INTEGER · NOT NULL
- `outcome` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_delivery_records_record_id(record_id)

## department_costs

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `dept_id` · INTEGER · NOT NULL · index · → departments.id
- `period` · VARCHAR(7) · NOT NULL · index
- `cost_type` · VARCHAR(16) · NOT NULL · index
- `amount` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (dept_id, period, cost_type) uq_dept_cost_period_type
- _index_ ix_department_costs_cost_type(cost_type)
- _index_ ix_department_costs_dept_id(dept_id)
- _index_ ix_department_costs_org_id(org_id)
- _index_ ix_department_costs_period(period)

## departments

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `code` · VARCHAR(32) · NOT NULL
- `name` · VARCHAR(64) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _unique_ (org_id, code) uq_dept_org_code
- _index_ ix_departments_org_id(org_id)

## disease_enrollments

- `id` · INTEGER · PK · NOT NULL
- `program_id` · INTEGER · NOT NULL · index · → disease_programs.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `status` · VARCHAR(16) · NOT NULL · index
- `enrolled_at` · VARCHAR(10) · NOT NULL
- `exited_at` · VARCHAR(10) · NOT NULL
- `outcome` · VARCHAR(16) · NOT NULL
- `outcome_note` · VARCHAR(512) · NOT NULL
- `exit_reason` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_disease_enrollments_org_id(org_id)
- _index_ ix_disease_enrollments_patient_id(patient_id)
- _index_ ix_disease_enrollments_program_id(program_id)
- _index_ ix_disease_enrollments_status(status)

## disease_path_records

- `id` · INTEGER · PK · NOT NULL
- `enrollment_id` · INTEGER · NOT NULL · index · → disease_enrollments.id
- `node_key` · VARCHAR(32) · NOT NULL · index
- `performed_at` · VARCHAR(10) · NOT NULL
- `operator_name` · VARCHAR(64) · NOT NULL
- `result` · VARCHAR(256) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_disease_path_records_enrollment_id(enrollment_id)
- _index_ ix_disease_path_records_node_key(node_key)

## disease_programs

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `description` · VARCHAR(512) · NOT NULL
- `org_id` · INTEGER · index · → organizations.id
- `path_nodes` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_disease_programs_active(active)
- _index_ ix_disease_programs_code(code) UNIQUE
- _index_ ix_disease_programs_org_id(org_id)

## drg_groups

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(16) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `base_weight` · FLOAT · NOT NULL
- `keywords` · VARCHAR(256) · NOT NULL
- `mdc` · VARCHAR(8) · NOT NULL · index
- `mdc_name` · VARCHAR(64) · NOT NULL
- `procedure_keywords` · VARCHAR(256) · NOT NULL
- `require_procedure` · BOOLEAN · NOT NULL
- `is_fallback` · BOOLEAN · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_drg_groups_code(code) UNIQUE
- _index_ ix_drg_groups_mdc(mdc)

## drug_rules

- `id` · INTEGER · PK · NOT NULL
- `drug_code` · VARCHAR(64) · NOT NULL · index
- `max_daily_dose` · FLOAT · NOT NULL
- `dose_unit` · VARCHAR(16) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `interactions` · VARCHAR(512) · NOT NULL
- `contraindicated_diagnoses` · VARCHAR(512) · NOT NULL
- `special_groups` · VARCHAR(64) · NOT NULL
- `renal_hepatic_note` · VARCHAR(512) · NOT NULL
- `review_points` · VARCHAR(512) · NOT NULL
- `antibiotic` · BOOLEAN · NOT NULL · index
- `ddd` · FLOAT · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_drug_rules_active(active)
- _index_ ix_drug_rules_antibiotic(antibiotic)
- _index_ ix_drug_rules_drug_code(drug_code) UNIQUE

## drug_shortages

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `patient_id` · INTEGER · index · → patients.id
- `drug_code` · VARCHAR(64) · NOT NULL
- `drug_name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `close_reason` · VARCHAR(256) · NOT NULL
- `closed_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_drug_shortages_org_id(org_id)
- _index_ ix_drug_shortages_patient_id(patient_id)
- _index_ ix_drug_shortages_status(status)

## drug_stocks

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `drug_code` · VARCHAR(64) · NOT NULL · index
- `drug_name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `threshold` · INTEGER · NOT NULL
- _unique_ (org_id, drug_code) uq_stock_org_drug
- _index_ ix_drug_stocks_drug_code(drug_code)
- _index_ ix_drug_stocks_org_id(org_id)

## dual_channel_apps

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `drug_name` · VARCHAR(128) · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `review_comment` · VARCHAR(512) · NOT NULL
- `reviewed_by` · INTEGER · → users.id
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_dual_channel_apps_patient_id(patient_id)
- _index_ ix_dual_channel_apps_status(status)

## duty_rosters

- `id` · INTEGER · PK · NOT NULL
- `center_type` · VARCHAR(16) · NOT NULL · index
- `duty_date` · VARCHAR(10) · NOT NULL · index
- `shift` · VARCHAR(16) · NOT NULL
- `doctor_name` · VARCHAR(64) · NOT NULL
- _index_ ix_duty_rosters_center_type(center_type)
- _index_ ix_duty_rosters_duty_date(duty_date)

## elderly_assessments

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · index · → organizations.id
- `adl_score` · INTEGER · NOT NULL
- `cognitive_score` · INTEGER · NOT NULL
- `tcm_constitution` · VARCHAR(32) · NOT NULL
- `care_level` · VARCHAR(16) · NOT NULL
- `assessed_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_elderly_assessments_org_id(org_id)
- _index_ ix_elderly_assessments_patient_id(patient_id)

## emergency_cases

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · → patients.id
- `caller_phone` · VARCHAR(20) · NOT NULL
- `location` · VARCHAR(256) · NOT NULL
- `symptom` · VARCHAR(512) · NOT NULL
- `ambulance_no` · VARCHAR(32) · NOT NULL
- `dest_org_id` · INTEGER · → organizations.id
- `channel_type` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `rescue_outcome` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_emergency_cases_channel_type(channel_type)
- _index_ ix_emergency_cases_rescue_outcome(rescue_outcome)
- _index_ ix_emergency_cases_status(status)

## emergency_milestones

- `id` · INTEGER · PK · NOT NULL
- `case_id` · INTEGER · NOT NULL · index · → emergency_cases.id
- `milestone` · VARCHAR(16) · NOT NULL
- `occurred_at` · VARCHAR(32) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (case_id, milestone) uq_emergency_milestone_case
- _index_ ix_emergency_milestones_case_id(case_id)

## emergency_resources

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `resource_type` · VARCHAR(16) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `min_quantity` · INTEGER · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL · index
- `contact` · VARCHAR(64) · NOT NULL
- `location` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_emergency_resources_expire_date(expire_date)
- _index_ ix_emergency_resources_org_id(org_id)
- _index_ ix_emergency_resources_resource_type(resource_type)

## emergency_vitals

- `id` · INTEGER · PK · NOT NULL
- `case_id` · INTEGER · NOT NULL · index · → emergency_cases.id
- `heart_rate` · FLOAT
- `sbp` · FLOAT
- `dbp` · FLOAT
- `spo2` · FLOAT
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_emergency_vitals_case_id(case_id)

## employee_changes

- `id` · INTEGER · PK · NOT NULL
- `employee_id` · INTEGER · NOT NULL · index · → employees.id
- `change_type` · VARCHAR(16) · NOT NULL · index
- `to_org_id` · INTEGER · → organizations.id
- `detail` · VARCHAR(256) · NOT NULL
- `effective_date` · VARCHAR(10) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_employee_changes_change_type(change_type)
- _index_ ix_employee_changes_employee_id(employee_id)

## employees

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `name` · VARCHAR(64) · NOT NULL
- `title` · VARCHAR(32) · NOT NULL
- `title_level` · VARCHAR(16) · NOT NULL · index
- `position` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `dept_id` · INTEGER · → departments.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_employees_org_id(org_id)
- _index_ ix_employees_status(status)
- _index_ ix_employees_title_level(title_level)

## encounters

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `doctor_name` · VARCHAR(64) · NOT NULL
- `encounter_type` · VARCHAR(16) · NOT NULL
- `diagnosis_code` · VARCHAR(64) · NOT NULL
- `diagnosis_name` · VARCHAR(256) · NOT NULL
- `summary` · VARCHAR(1024) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_encounters_org_id(org_id)
- _index_ ix_encounters_patient_id(patient_id)

## esb_endpoints

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `system_type` · VARCHAR(16) · NOT NULL · index
- `direction` · VARCHAR(8) · NOT NULL · index
- `auth_token_hash` · VARCHAR(200) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `rate_limit_per_min` · INTEGER · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_esb_endpoints_active(active)
- _index_ ix_esb_endpoints_code(code) UNIQUE
- _index_ ix_esb_endpoints_direction(direction)
- _index_ ix_esb_endpoints_system_type(system_type)

## esb_flow_runs

- `id` · INTEGER · PK · NOT NULL
- `flow_id` · INTEGER · NOT NULL · index · → esb_flows.id
- `message_id` · INTEGER · NOT NULL · index · → esb_messages.id
- `status` · VARCHAR(16) · NOT NULL · index
- `step_results` · JSON · NOT NULL
- `error` · VARCHAR(1024) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_esb_flow_runs_created_at(created_at)
- _index_ ix_esb_flow_runs_flow_id(flow_id)
- _index_ ix_esb_flow_runs_message_id(message_id)
- _index_ ix_esb_flow_runs_status(status)

## esb_flows

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `steps` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_esb_flows_active(active)
- _index_ ix_esb_flows_code(code) UNIQUE

## esb_messages

- `id` · INTEGER · PK · NOT NULL
- `endpoint_id` · INTEGER · NOT NULL · index · → esb_endpoints.id
- `msg_type` · VARCHAR(32) · NOT NULL · index
- `payload` · JSON · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `retry_count` · INTEGER · NOT NULL
- `max_retries` · INTEGER · NOT NULL
- `last_error` · VARCHAR(1024) · NOT NULL
- `next_retry_at` · DATETIME
- `created_at` · DATETIME · NOT NULL · index
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_esb_messages_created_at(created_at)
- _index_ ix_esb_messages_endpoint_id(endpoint_id)
- _index_ ix_esb_messages_msg_type(msg_type)
- _index_ ix_esb_messages_status(status)

## exam_reports

- `id` · INTEGER · PK · NOT NULL
- `request_id` · INTEGER · NOT NULL · index · → exam_requests.id
- `finding` · VARCHAR(2048) · NOT NULL
- `conclusion` · VARCHAR(1024) · NOT NULL
- `critical` · BOOLEAN · NOT NULL
- `critical_status` · VARCHAR(16) · NOT NULL · index
- `reported_by` · VARCHAR(64) · NOT NULL
- `reported_at` · DATETIME · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_exam_reports_created_at(created_at)
- _index_ ix_exam_reports_critical_status(critical_status)
- _index_ ix_exam_reports_request_id(request_id) UNIQUE

## exam_requests

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `center_type` · VARCHAR(16) · NOT NULL · index
- `item_code` · VARCHAR(64) · NOT NULL · index
- `item_name` · VARCHAR(128) · NOT NULL
- `clinical_info` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `recognized_from_id` · INTEGER · → exam_requests.id
- `recognition_declined_reason` · VARCHAR(256) · NOT NULL
- `sample_status` · VARCHAR(16) · NOT NULL
- `claimed_by` · VARCHAR(64) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_exam_requests_center_type(center_type)
- _index_ ix_exam_requests_item_code(item_code)
- _index_ ix_exam_requests_patient_id(patient_id)
- _index_ ix_exam_requests_status(status)

## exam_resources

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `center_type` · VARCHAR(16) · NOT NULL · index
- `item_name` · VARCHAR(128) · NOT NULL
- `device` · VARCHAR(128) · NOT NULL
- `price` · NUMERIC(14, 2) · NOT NULL
- `duration_min` · INTEGER · NOT NULL
- `notes` · VARCHAR(512) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_exam_resources_center_type(center_type)
- _index_ ix_exam_resources_org_id(org_id)

## exchange_logs

- `id` · INTEGER · PK · NOT NULL
- `source_system` · VARCHAR(64) · NOT NULL · index
- `message_type` · VARCHAR(32) · NOT NULL · index
- `direction` · VARCHAR(8) · NOT NULL
- `success` · BOOLEAN · NOT NULL · index
- `error_detail` · VARCHAR(1024) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_exchange_logs_created_at(created_at)
- _index_ ix_exchange_logs_message_type(message_type)
- _index_ ix_exchange_logs_source_system(source_system)
- _index_ ix_exchange_logs_success(success)

## fd_contract_services

- `id` · INTEGER · PK · NOT NULL
- `contract_id` · INTEGER · NOT NULL · index · → fd_contracts.id
- `service_type` · VARCHAR(16) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_fd_contract_services_contract_id(contract_id)

## fd_contracts

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `doctor_name` · VARCHAR(64) · NOT NULL
- `package` · VARCHAR(16) · NOT NULL
- `signed_date` · VARCHAR(10) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (patient_id, org_id) uq_contract_patient_org
- _index_ ix_fd_contracts_org_id(org_id)
- _index_ ix_fd_contracts_patient_id(patient_id)

## finance_entries

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `period` · VARCHAR(7) · NOT NULL · index
- `category` · VARCHAR(8) · NOT NULL
- `item` · VARCHAR(128) · NOT NULL
- `amount` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_finance_entries_org_id(org_id)
- _index_ ix_finance_entries_period(period)

## followup_tasks

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `category` · VARCHAR(16) · NOT NULL · index
- `source_id` · INTEGER · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `due_date` · VARCHAR(10) · NOT NULL · index
- `assigned_to` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `result` · VARCHAR(1024) · NOT NULL
- `completed_at` · DATETIME
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_followup_tasks_category(category)
- _index_ ix_followup_tasks_created_at(created_at)
- _index_ ix_followup_tasks_due_date(due_date)
- _index_ ix_followup_tasks_org_id(org_id)
- _index_ ix_followup_tasks_patient_id(patient_id)
- _index_ ix_followup_tasks_source_id(source_id)
- _index_ ix_followup_tasks_status(status)

## followups

- `id` · INTEGER · PK · NOT NULL
- `chronic_id` · INTEGER · NOT NULL · index · → chronic_patients.id
- `sbp` · FLOAT
- `dbp` · FLOAT
- `glucose` · FLOAT
- `metrics` · JSON · NOT NULL
- `guidance` · VARCHAR(1024) · NOT NULL
- `next_due` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_followups_chronic_id(chronic_id)

## fund_distributions

- `id` · INTEGER · PK · NOT NULL
- `settlement_id` · INTEGER · NOT NULL · index · → fund_settlements.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `score` · FLOAT · NOT NULL
- `score_detail` · JSON · NOT NULL
- `weight` · FLOAT · NOT NULL
- `share_pct` · FLOAT · NOT NULL
- `amount` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_fund_distributions_org_id(org_id)
- _index_ ix_fund_distributions_settlement_id(settlement_id)

## fund_periods

- `id` · INTEGER · PK · NOT NULL
- `pool_id` · INTEGER · NOT NULL · index · → fund_pools.id
- `period` · VARCHAR(7) · NOT NULL · index
- `actual_amount` · NUMERIC(14, 2) · NOT NULL
- `source` · VARCHAR(16) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (pool_id, period) uq_fund_period
- _index_ ix_fund_periods_period(period)
- _index_ ix_fund_periods_pool_id(pool_id)

## fund_pools

- `id` · INTEGER · PK · NOT NULL
- `year` · INTEGER · NOT NULL · index
- `insurance_type` · VARCHAR(16) · NOT NULL · index
- `org_group_id` · INTEGER · index · → org_groups.id
- `total_amount` · NUMERIC(14, 2) · NOT NULL
- `prepay_ratio_pct` · NUMERIC(14, 2) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _unique_ (year, insurance_type, org_group_id) uq_fund_pool_scope
- _index_ ix_fund_pools_insurance_type(insurance_type)
- _index_ ix_fund_pools_org_group_id(org_group_id)
- _index_ ix_fund_pools_status(status)
- _index_ ix_fund_pools_year(year)
- _index_ uq_fund_pool_global(year, insurance_type) UNIQUE

## fund_prepayments

- `id` · INTEGER · PK · NOT NULL
- `pool_id` · INTEGER · NOT NULL · index · → fund_pools.id
- `batch_no` · VARCHAR(32) · NOT NULL
- `amount` · NUMERIC(14, 2) · NOT NULL
- `paid_date` · VARCHAR(10) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_fund_prepayments_pool_id(pool_id)

## fund_settlements

- `id` · INTEGER · PK · NOT NULL
- `pool_id` · INTEGER · NOT NULL · index · → fund_pools.id
- `total_income` · NUMERIC(14, 2) · NOT NULL
- `total_expense` · NUMERIC(14, 2) · NOT NULL
- `balance` · NUMERIC(14, 2) · NOT NULL
- `overrun_action` · VARCHAR(16) · NOT NULL
- `formula_expr` · VARCHAR(256) · NOT NULL
- `score_basis` · VARCHAR(128) · NOT NULL
- `settled_at` · DATETIME · NOT NULL
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _unique_ (pool_id) uq_fund_settlement_pool
- _index_ ix_fund_settlements_created_at(created_at)
- _index_ ix_fund_settlements_pool_id(pool_id) UNIQUE

## health_articles

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `category` · VARCHAR(32) · NOT NULL
- `content` · VARCHAR(4096) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_health_articles_status(status)

## health_monitor_records

- `id` · INTEGER · PK · NOT NULL
- `domain` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `indicator` · VARCHAR(128) · NOT NULL
- `value` · FLOAT · NOT NULL
- `threshold` · FLOAT · NOT NULL
- `exceeded` · BOOLEAN · NOT NULL · index
- `record_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_health_monitor_records_created_at(created_at)
- _index_ ix_health_monitor_records_domain(domain)
- _index_ ix_health_monitor_records_exceeded(exceeded)

## high_value_consumables

- `id` · INTEGER · PK · NOT NULL
- `barcode` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `spec` · VARCHAR(64) · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `supplier_id` · INTEGER · → suppliers.id
- `batch_no` · VARCHAR(64) · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL · index
- `unit_price` · NUMERIC(14, 2) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `used_patient_id` · INTEGER · index · → patients.id
- `used_surgery_id` · INTEGER · index · → surgery_requests.id
- `used_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_high_value_consumables_barcode(barcode) UNIQUE
- _index_ ix_high_value_consumables_expire_date(expire_date)
- _index_ ix_high_value_consumables_org_id(org_id)
- _index_ ix_high_value_consumables_status(status)
- _index_ ix_high_value_consumables_used_patient_id(used_patient_id)
- _index_ ix_high_value_consumables_used_surgery_id(used_surgery_id)

## home_visit_orders

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `contract_id` · INTEGER · → fd_contracts.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `service_type` · VARCHAR(16) · NOT NULL · index
- `demand` · VARCHAR(512) · NOT NULL
- `address` · VARCHAR(256) · NOT NULL
- `expect_date` · VARCHAR(10) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `assignee_name` · VARCHAR(64) · NOT NULL
- `dispatched_at` · DATETIME
- `service_note` · VARCHAR(512) · NOT NULL
- `completed_at` · DATETIME
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_home_visit_orders_org_id(org_id)
- _index_ ix_home_visit_orders_patient_id(patient_id)
- _index_ ix_home_visit_orders_service_type(service_type)
- _index_ ix_home_visit_orders_status(status)

## improvement_tasks

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `indicator_key` · VARCHAR(32) · NOT NULL · index
- `problem` · VARCHAR(512) · NOT NULL
- `measures` · VARCHAR(1024) · NOT NULL
- `owner_name` · VARCHAR(64) · NOT NULL
- `due_date` · VARCHAR(10) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `completion_note` · VARCHAR(512) · NOT NULL
- `completed_at` · DATETIME
- `verify_comment` · VARCHAR(512) · NOT NULL
- `verified_by` · VARCHAR(64) · NOT NULL
- `verified_at` · DATETIME
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_improvement_tasks_due_date(due_date)
- _index_ ix_improvement_tasks_indicator_key(indicator_key)
- _index_ ix_improvement_tasks_org_id(org_id)
- _index_ ix_improvement_tasks_status(status)

## infection_reports

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `infection_site` · VARCHAR(16) · NOT NULL · index
- `pathogen` · VARCHAR(128) · NOT NULL
- `note` · VARCHAR(1024) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `reported_by` · VARCHAR(64) · NOT NULL
- `report_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_infection_reports_infection_site(infection_site)
- _index_ ix_infection_reports_org_id(org_id)
- _index_ ix_infection_reports_patient_id(patient_id)
- _index_ ix_infection_reports_status(status)

## infectious_cases

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `disease_code` · VARCHAR(64) · NOT NULL · index
- `disease_name` · VARCHAR(128) · NOT NULL
- `category` · VARCHAR(4) · NOT NULL
- `onset_date` · VARCHAR(10) · NOT NULL · index
- `reported_at` · DATETIME · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_infectious_cases_created_at(created_at)
- _index_ ix_infectious_cases_disease_code(disease_code)
- _index_ ix_infectious_cases_onset_date(onset_date)
- _index_ ix_infectious_cases_org_id(org_id)

## infectious_diseases

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `category` · VARCHAR(4) · NOT NULL · index
- `report_hours` · INTEGER · NOT NULL
- _index_ ix_infectious_diseases_category(category)
- _index_ ix_infectious_diseases_code(code) UNIQUE

## informed_consents

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `consent_type` · VARCHAR(16) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `content` · VARCHAR(8192) · NOT NULL
- `template_version` · VARCHAR(16) · NOT NULL
- `related_type` · VARCHAR(32) · NOT NULL · index
- `related_id` · INTEGER · NOT NULL · index
- `doctor_name` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `signer_name` · VARCHAR(64) · NOT NULL
- `signer_relation` · VARCHAR(16) · NOT NULL
- `signed_at` · DATETIME
- `refuse_reason` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_informed_consents_consent_type(consent_type)
- _index_ ix_informed_consents_created_at(created_at)
- _index_ ix_informed_consents_org_id(org_id)
- _index_ ix_informed_consents_patient_id(patient_id)
- _index_ ix_informed_consents_related_id(related_id)
- _index_ ix_informed_consents_related_type(related_type)
- _index_ ix_informed_consents_status(status)

## inpatient_orders

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · NOT NULL · index · → admissions.id
- `order_type` · VARCHAR(8) · NOT NULL
- `content` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by_name` · VARCHAR(64) · NOT NULL
- `stopped_by_name` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- `stopped_at` · DATETIME
- _index_ ix_inpatient_orders_admission_id(admission_id)
- _index_ ix_inpatient_orders_status(status)

## insurance_settlements

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `settle_type` · VARCHAR(16) · NOT NULL
- `total_amount` · NUMERIC(14, 2) · NOT NULL
- `insurance_pay` · NUMERIC(14, 2) · NOT NULL
- `self_pay` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_insurance_settlements_org_id(org_id)
- _index_ ix_insurance_settlements_patient_id(patient_id)

## job_runs

- `id` · INTEGER · PK · NOT NULL
- `job_name` · VARCHAR(64) · NOT NULL · index
- `trigger` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `message` · VARCHAR(1024) · NOT NULL
- `affected` · INTEGER · NOT NULL
- `duration_ms` · INTEGER · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_job_runs_created_at(created_at)
- _index_ ix_job_runs_job_name(job_name)
- _index_ ix_job_runs_status(status)

## knowledge_entries

- `id` · INTEGER · PK · NOT NULL
- `category` · VARCHAR(32) · NOT NULL · index
- `title` · VARCHAR(256) · NOT NULL · index
- `body` · VARCHAR(4096) · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_knowledge_entries_category(category)
- _index_ ix_knowledge_entries_title(title)

## live_feedbacks

- `id` · INTEGER · PK · NOT NULL
- `session_id` · INTEGER · NOT NULL · index · → live_sessions.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `rating` · INTEGER · NOT NULL
- `comment` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (session_id, user_id) uq_live_feedback
- _index_ ix_live_feedbacks_session_id(session_id)
- _index_ ix_live_feedbacks_user_id(user_id)

## live_sessions

- `id` · INTEGER · PK · NOT NULL
- `course_id` · INTEGER · → courses.id
- `title` · VARCHAR(256) · NOT NULL
- `speaker` · VARCHAR(64) · NOT NULL
- `planned_at` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `review_comment` · VARCHAR(256) · NOT NULL
- `recording_url` · VARCHAR(512) · NOT NULL
- `recorded_at` · DATETIME
- `requested_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_live_sessions_status(status)

## material_purchases

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `dept_id` · INTEGER · → departments.id
- `item_name` · VARCHAR(128) · NOT NULL
- `spec` · VARCHAR(64) · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `estimated_price` · NUMERIC(14, 2) · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `supplier_id` · INTEGER · → suppliers.id
- `contract_no` · VARCHAR(64) · NOT NULL
- `contract_amount` · NUMERIC(14, 2) · NOT NULL
- `received_quantity` · INTEGER · NOT NULL
- `received_note` · VARCHAR(512) · NOT NULL
- `requested_by` · INTEGER · NOT NULL · → users.id
- `approved_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_material_purchases_created_at(created_at)
- _index_ ix_material_purchases_org_id(org_id)
- _index_ ix_material_purchases_status(status)

## maternal_records

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `lmp` · VARCHAR(10) · NOT NULL
- `edc` · VARCHAR(10) · NOT NULL
- `gravidity` · INTEGER · NOT NULL
- `parity` · INTEGER · NOT NULL
- `high_risk` · BOOLEAN · NOT NULL
- `risk_factors` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (patient_id) uq_maternal_patient
- _index_ ix_maternal_records_patient_id(patient_id)
- _index_ ix_maternal_records_status(status)

## maternal_visits

- `id` · INTEGER · PK · NOT NULL
- `record_id` · INTEGER · NOT NULL · index · → maternal_records.id
- `visit_type` · VARCHAR(16) · NOT NULL
- `gest_week` · INTEGER
- `bp` · VARCHAR(16) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `visit_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_maternal_visits_created_at(created_at)
- _index_ ix_maternal_visits_record_id(record_id)

## medical_certs

- `id` · INTEGER · PK · NOT NULL
- `cert_type` · VARCHAR(8) · NOT NULL · index
- `cert_no` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `gender` · VARCHAR(8) · NOT NULL
- `event_date` · VARCHAR(10) · NOT NULL
- `detail` · VARCHAR(512) · NOT NULL
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `patient_id` · INTEGER · → patients.id
- `child_id` · INTEGER · → child_records.id
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_medical_certs_cert_no(cert_no) UNIQUE
- _index_ ix_medical_certs_cert_type(cert_type)

## medical_records

- `id` · INTEGER · PK · NOT NULL
- `encounter_id` · INTEGER · NOT NULL · index · → encounters.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `doctor_name` · VARCHAR(64) · NOT NULL · index
- `chief_complaint` · VARCHAR(256) · NOT NULL
- `present_illness` · VARCHAR(2048) · NOT NULL
- `past_history` · VARCHAR(1024) · NOT NULL
- `physical_exam` · VARCHAR(1024) · NOT NULL
- `diagnosis_basis` · VARCHAR(1024) · NOT NULL
- `treatment_plan` · VARCHAR(1024) · NOT NULL
- `qc_score` · INTEGER · NOT NULL · index
- `qc_grade` · VARCHAR(4) · NOT NULL · index
- `qc_defects` · JSON · NOT NULL
- `created_by` · INTEGER · NOT NULL · index · → users.id
- `created_at` · DATETIME · NOT NULL · index
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_medical_records_created_at(created_at)
- _index_ ix_medical_records_created_by(created_by)
- _index_ ix_medical_records_doctor_name(doctor_name)
- _index_ ix_medical_records_encounter_id(encounter_id) UNIQUE
- _index_ ix_medical_records_org_id(org_id)
- _index_ ix_medical_records_qc_grade(qc_grade)
- _index_ ix_medical_records_qc_score(qc_score)

## medical_wastes

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `waste_type` · VARCHAR(16) · NOT NULL
- `weight_kg` · FLOAT · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `handler_name` · VARCHAR(64) · NOT NULL
- `collected_date` · VARCHAR(10) · NOT NULL · index
- `handed_over_at` · DATETIME
- `trace_code` · VARCHAR(32) · NOT NULL · index
- `source_location_id` · INTEGER · index · → waste_locations.id
- `storage_location_id` · INTEGER · index · → waste_locations.id
- `handler_employee_id` · INTEGER · index · → employees.id
- `stored_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_medical_wastes_collected_date(collected_date)
- _index_ ix_medical_wastes_handler_employee_id(handler_employee_id)
- _index_ ix_medical_wastes_org_id(org_id)
- _index_ ix_medical_wastes_source_location_id(source_location_id)
- _index_ ix_medical_wastes_status(status)
- _index_ ix_medical_wastes_storage_location_id(storage_location_id)
- _index_ ix_medical_wastes_trace_code(trace_code) UNIQUE

## newborn_screenings

- `id` · INTEGER · PK · NOT NULL
- `child_id` · INTEGER · NOT NULL · index · → child_records.id
- `item` · VARCHAR(16) · NOT NULL
- `result` · VARCHAR(16) · NOT NULL
- `screen_date` · VARCHAR(10) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_newborn_screenings_child_id(child_id)

## notifications

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · index · → users.id
- `resident_account_id` · INTEGER · index · → resident_accounts.id
- `category` · VARCHAR(24) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `body` · VARCHAR(1024) · NOT NULL
- `link_type` · VARCHAR(32) · NOT NULL
- `link_id` · INTEGER · NOT NULL
- `read_at` · DATETIME · index
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_notifications_category(category)
- _index_ ix_notifications_created_at(created_at)
- _index_ ix_notifications_read_at(read_at)
- _index_ ix_notifications_resident_account_id(resident_account_id)
- _index_ ix_notifications_user_id(user_id)

## nursing_records

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · index · → admissions.id
- `encounter_id` · INTEGER · index · → encounters.id
- `nursing_level` · VARCHAR(16) · NOT NULL · index
- `content` · VARCHAR(2048) · NOT NULL
- `nurse_name` · VARCHAR(64) · NOT NULL
- `recorded_at` · VARCHAR(16) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_nursing_records_admission_id(admission_id)
- _index_ ix_nursing_records_created_at(created_at)
- _index_ ix_nursing_records_encounter_id(encounter_id)
- _index_ ix_nursing_records_nursing_level(nursing_level)

## official_docs

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `doc_type` · VARCHAR(16) · NOT NULL
- `body` · VARCHAR(4096) · NOT NULL
- `issuer` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_official_docs_status(status)

## online_consults

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `consult_type` · VARCHAR(16) · NOT NULL
- `question` · VARCHAR(1024) · NOT NULL
- `reply` · VARCHAR(2048) · NOT NULL
- `doctor_name` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `prescription_id` · INTEGER · → prescriptions.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_online_consults_patient_id(patient_id)
- _index_ ix_online_consults_status(status)

## operating_rooms

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `name` · VARCHAR(64) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (org_id, name) uq_or_org_name
- _index_ ix_operating_rooms_active(active)
- _index_ ix_operating_rooms_org_id(org_id)

## org_group_members

- `id` · INTEGER · PK · NOT NULL
- `group_id` · INTEGER · NOT NULL · index · → org_groups.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `joined_at` · DATETIME · NOT NULL
- _unique_ (group_id, org_id) uq_org_group_member
- _index_ ix_org_group_members_group_id(group_id)
- _index_ ix_org_group_members_org_id(org_id)

## org_groups

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL · index
- `group_type` · VARCHAR(16) · NOT NULL · index
- `lead_org_id` · INTEGER · → organizations.id
- `note` · VARCHAR(256) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_org_groups_active(active)
- _index_ ix_org_groups_group_type(group_type)
- _index_ ix_org_groups_name(name) UNIQUE

## organizations

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(128) · NOT NULL · index
- `org_type` · VARCHAR(32) · NOT NULL
- `level` · VARCHAR(16) · NOT NULL
- `parent_id` · INTEGER · → organizations.id
- `address` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_organizations_name(name) UNIQUE

## outbound_visits

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `visit_date` · VARCHAR(10) · NOT NULL · index
- `external_org_name` · VARCHAR(128) · NOT NULL
- `external_org_level` · VARCHAR(16) · NOT NULL · index
- `visit_type` · VARCHAR(16) · NOT NULL · index
- `diagnosis_name` · VARCHAR(256) · NOT NULL
- `total_amount` · NUMERIC(14, 2) · NOT NULL
- `insurance_pay` · NUMERIC(14, 2) · NOT NULL
- `referral_id` · INTEGER · → referrals.id
- `source` · VARCHAR(20) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_outbound_visits_external_org_level(external_org_level)
- _index_ ix_outbound_visits_patient_id(patient_id)
- _index_ ix_outbound_visits_source(source)
- _index_ ix_outbound_visits_visit_date(visit_date)
- _index_ ix_outbound_visits_visit_type(visit_type)

## pathogen_monitors

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `pathogen_name` · VARCHAR(128) · NOT NULL · index
- `specimen_type` · VARCHAR(64) · NOT NULL
- `tested_count` · INTEGER · NOT NULL
- `positive_count` · INTEGER · NOT NULL
- `record_date` · VARCHAR(10) · NOT NULL · index
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_pathogen_monitors_org_id(org_id)
- _index_ ix_pathogen_monitors_pathogen_name(pathogen_name)
- _index_ ix_pathogen_monitors_record_date(record_date)

## pathology_specimens

- `id` · INTEGER · PK · NOT NULL
- `request_id` · INTEGER · NOT NULL · index · → exam_requests.id
- `specimen_no` · VARCHAR(32) · NOT NULL · index
- `site` · VARCHAR(128) · NOT NULL
- `excised_at` · VARCHAR(19) · NOT NULL
- `fixed_at` · VARCHAR(19) · NOT NULL
- `fixative` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `reject_reason` · VARCHAR(256) · NOT NULL
- `received_by` · VARCHAR(64) · NOT NULL
- `block_count` · INTEGER · NOT NULL
- `slide_count` · INTEGER · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (specimen_no) uq_pathology_specimen_no
- _index_ ix_pathology_specimens_request_id(request_id)
- _index_ ix_pathology_specimens_specimen_no(specimen_no)
- _index_ ix_pathology_specimens_status(status)

## patients

- `id` · INTEGER · PK · NOT NULL
- `ehc_no` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL · index
- `id_card` · VARCHAR(18) · NOT NULL · index
- `gender` · VARCHAR(8) · NOT NULL
- `birth_date` · VARCHAR(10) · NOT NULL
- `phone` · VARCHAR(20) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (id_card) uq_patient_id_card
- _index_ ix_patients_ehc_no(ehc_no) UNIQUE
- _index_ ix_patients_id_card(id_card)
- _index_ ix_patients_name(name)

## payment_orders

- `id` · INTEGER · PK · NOT NULL
- `settlement_id` · INTEGER · NOT NULL · index · → settlements.id
- `channel` · VARCHAR(16) · NOT NULL · index
- `amount` · NUMERIC(14, 2) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `trade_no` · VARCHAR(64) · NOT NULL · index
- `refunded_amount` · NUMERIC(14, 2) · NOT NULL
- `fail_reason` · VARCHAR(256) · NOT NULL
- `paid_at` · DATETIME · index
- `refunded_at` · DATETIME
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_payment_orders_channel(channel)
- _index_ ix_payment_orders_created_at(created_at)
- _index_ ix_payment_orders_paid_at(paid_at)
- _index_ ix_payment_orders_settlement_id(settlement_id)
- _index_ ix_payment_orders_status(status)
- _index_ ix_payment_orders_trade_no(trade_no)

## payroll_records

- `id` · INTEGER · PK · NOT NULL
- `employee_id` · INTEGER · NOT NULL · index · → employees.id
- `period` · VARCHAR(7) · NOT NULL · index
- `base_salary` · NUMERIC(14, 2) · NOT NULL
- `perf_bonus` · NUMERIC(14, 2) · NOT NULL
- `perf_coefficient` · FLOAT · NOT NULL
- `total` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (employee_id, period) uq_payroll_emp_period
- _index_ ix_payroll_records_employee_id(employee_id)
- _index_ ix_payroll_records_period(period)

## performance_formulas

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `expression` · VARCHAR(512) · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `higher_is_better` · BOOLEAN · NOT NULL
- `weight` · FLOAT · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_performance_formulas_active(active)
- _index_ ix_performance_formulas_key(key) UNIQUE

## performance_indicators

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `weight` · FLOAT · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_performance_indicators_key(key) UNIQUE

## permissions

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(320) · NOT NULL · index
- `method` · VARCHAR(8) · NOT NULL
- `path` · VARCHAR(300) · NOT NULL · index
- `module` · VARCHAR(64) · NOT NULL · index
- `builtin_roles` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_permissions_code(code) UNIQUE
- _index_ ix_permissions_module(module)
- _index_ ix_permissions_path(path)

## ph_event_actions

- `id` · INTEGER · PK · NOT NULL
- `event_id` · INTEGER · NOT NULL · index · → ph_events.id
- `action` · VARCHAR(512) · NOT NULL
- `actor` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_ph_event_actions_event_id(event_id)

## ph_events

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `level` · VARCHAR(4) · NOT NULL
- `disease_name` · VARCHAR(128) · NOT NULL
- `description` · VARCHAR(1024) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_ph_events_status(status)

## physical_exams

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `package_name` · VARCHAR(128) · NOT NULL
- `exam_date` · VARCHAR(10) · NOT NULL
- `summary` · VARCHAR(1024) · NOT NULL
- `abnormal_items` · VARCHAR(512) · NOT NULL
- `has_abnormal` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_physical_exams_has_abnormal(has_abnormal)
- _index_ ix_physical_exams_patient_id(patient_id)

## prenatal_screenings

- `id` · INTEGER · PK · NOT NULL
- `record_id` · INTEGER · NOT NULL · index · → maternal_records.id
- `screen_type` · VARCHAR(16) · NOT NULL · index
- `screen_date` · VARCHAR(10) · NOT NULL
- `gest_week` · INTEGER
- `result` · VARCHAR(16) · NOT NULL · index
- `indicator` · VARCHAR(256) · NOT NULL
- `conclusion` · VARCHAR(512) · NOT NULL
- `flagged_high_risk` · BOOLEAN · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_prenatal_screenings_flagged_high_risk(flagged_high_risk)
- _index_ ix_prenatal_screenings_record_id(record_id)
- _index_ ix_prenatal_screenings_result(result)
- _index_ ix_prenatal_screenings_screen_type(screen_type)

## prescription_comments

- `id` · INTEGER · PK · NOT NULL
- `prescription_id` · INTEGER · NOT NULL · index · → prescriptions.id
- `grade` · VARCHAR(16) · NOT NULL · index
- `issues` · VARCHAR(256) · NOT NULL
- `comment` · VARCHAR(1024) · NOT NULL
- `reviewer_id` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _unique_ (prescription_id) uq_rx_comment_prescription
- _index_ ix_prescription_comments_grade(grade)
- _index_ ix_prescription_comments_prescription_id(prescription_id)

## prescription_items

- `id` · INTEGER · PK · NOT NULL
- `prescription_id` · INTEGER · NOT NULL · index · → prescriptions.id
- `drug_code` · VARCHAR(64) · NOT NULL
- `drug_name` · VARCHAR(128) · NOT NULL
- `daily_dose` · FLOAT · NOT NULL
- `days` · INTEGER · NOT NULL
- _index_ ix_prescription_items_prescription_id(prescription_id)

## prescriptions

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `diagnosis_name` · VARCHAR(256) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `review_comment` · VARCHAR(1024) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_prescriptions_patient_id(patient_id)
- _index_ ix_prescriptions_status(status)

## print_templates

- `id` · INTEGER · PK · NOT NULL
- `doc_type` · VARCHAR(32) · NOT NULL · index
- `header_org_name` · VARCHAR(128) · NOT NULL
- `footer_note` · VARCHAR(256) · NOT NULL
- `show_qr` · BOOLEAN · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_print_templates_doc_type(doc_type) UNIQUE

## progress_notes

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · NOT NULL · index · → admissions.id
- `note_type` · VARCHAR(16) · NOT NULL · index
- `content` · VARCHAR(4096) · NOT NULL
- `doctor_name` · VARCHAR(64) · NOT NULL
- `recorded_at` · VARCHAR(16) · NOT NULL
- `created_by` · INTEGER · NOT NULL · index · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_progress_notes_admission_id(admission_id)
- _index_ ix_progress_notes_created_at(created_at)
- _index_ ix_progress_notes_created_by(created_by)
- _index_ ix_progress_notes_note_type(note_type)

## project_milestones

- `id` · INTEGER · PK · NOT NULL
- `project_id` · INTEGER · NOT NULL · index · → admin_projects.id
- `name` · VARCHAR(256) · NOT NULL
- `due_date` · VARCHAR(10) · NOT NULL · index
- `done` · BOOLEAN · NOT NULL · index
- `done_date` · VARCHAR(10) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_project_milestones_done(done)
- _index_ ix_project_milestones_due_date(due_date)
- _index_ ix_project_milestones_project_id(project_id)

## purchase_orders

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `supplier_id` · INTEGER · NOT NULL · → suppliers.id
- `item_type` · VARCHAR(16) · NOT NULL
- `item_code` · VARCHAR(64) · NOT NULL
- `item_name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `requested_by` · INTEGER · NOT NULL · → users.id
- `approved_by` · INTEGER · → users.id
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_purchase_orders_org_id(org_id)
- _index_ ix_purchase_orders_status(status)

## qc_records

- `id` · INTEGER · PK · NOT NULL
- `center_type` · VARCHAR(16) · NOT NULL · index
- `item` · VARCHAR(128) · NOT NULL
- `result` · VARCHAR(8) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `record_date` · VARCHAR(10) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_qc_records_center_type(center_type)
- _index_ ix_qc_records_created_at(created_at)

## qc_rules

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `target_table` · VARCHAR(64) · NOT NULL · index
- `rule_type` · VARCHAR(16) · NOT NULL · index
- `config` · JSON · NOT NULL
- `severity` · VARCHAR(8) · NOT NULL · index
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_qc_rules_active(active)
- _index_ ix_qc_rules_code(code) UNIQUE
- _index_ ix_qc_rules_rule_type(rule_type)
- _index_ ix_qc_rules_severity(severity)
- _index_ ix_qc_rules_target_table(target_table)

## recognition_items

- `id` · INTEGER · PK · NOT NULL
- `item_code` · VARCHAR(64) · NOT NULL · index
- `item_name` · VARCHAR(128) · NOT NULL
- `center_type` · VARCHAR(16) · NOT NULL · index
- `mutual_scope` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_recognition_items_center_type(center_type)
- _index_ ix_recognition_items_item_code(item_code) UNIQUE

## reconciliation_batches

- `id` · INTEGER · PK · NOT NULL
- `date` · VARCHAR(10) · NOT NULL · index
- `total_orders` · INTEGER · NOT NULL
- `total_amount` · NUMERIC(14, 2) · NOT NULL
- `matched` · INTEGER · NOT NULL
- `unmatched` · INTEGER · NOT NULL
- `diff_amount` · NUMERIC(14, 2) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_reconciliation_batches_date(date)

## reconciliation_diffs

- `id` · INTEGER · PK · NOT NULL
- `batch_id` · INTEGER · NOT NULL · index · → reconciliation_batches.id
- `order_id` · INTEGER · → payment_orders.id
- `trade_no` · VARCHAR(64) · NOT NULL · index
- `diff_type` · VARCHAR(20) · NOT NULL · index
- `local_amount` · NUMERIC(14, 2) · NOT NULL
- `remote_amount` · NUMERIC(14, 2) · NOT NULL
- `detail` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_reconciliation_diffs_batch_id(batch_id)
- _index_ ix_reconciliation_diffs_diff_type(diff_type)
- _index_ ix_reconciliation_diffs_trade_no(trade_no)

## record_qc_rules

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `check_field` · VARCHAR(32) · NOT NULL · index
- `rule` · VARCHAR(24) · NOT NULL · index
- `config` · JSON · NOT NULL
- `deduct_points` · INTEGER · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_record_qc_rules_active(active)
- _index_ ix_record_qc_rules_check_field(check_field)
- _index_ ix_record_qc_rules_code(code) UNIQUE
- _index_ ix_record_qc_rules_rule(rule)

## record_qcs

- `id` · INTEGER · PK · NOT NULL
- `target_type` · VARCHAR(16) · NOT NULL · index
- `target_id` · INTEGER · NOT NULL · index
- `score` · INTEGER · NOT NULL
- `grade` · VARCHAR(4) · NOT NULL
- `defects` · VARCHAR(1024) · NOT NULL
- `qc_by` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_record_qcs_target_id(target_id)
- _index_ ix_record_qcs_target_type(target_type)

## referral_certs

- `id` · INTEGER · PK · NOT NULL
- `referral_id` · INTEGER · NOT NULL · → referrals.id
- `cert_no` · VARCHAR(32) · NOT NULL
- `issued_at` · DATETIME · NOT NULL
- _unique_ (cert_no)
- _unique_ (referral_id)

## referrals

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `to_org_id` · INTEGER · NOT NULL · → organizations.id
- `direction` · VARCHAR(8) · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_referrals_patient_id(patient_id)
- _index_ ix_referrals_status(status)

## report_revisions

- `id` · INTEGER · PK · NOT NULL
- `report_id` · INTEGER · NOT NULL · index · → exam_reports.id
- `prev_conclusion` · VARCHAR(1024) · NOT NULL
- `prev_finding` · VARCHAR(2048) · NOT NULL
- `prev_critical` · BOOLEAN · NOT NULL
- `revised_by` · VARCHAR(64) · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_report_revisions_report_id(report_id)

## report_templates

- `id` · INTEGER · PK · NOT NULL
- `center_type` · VARCHAR(16) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `content` · VARCHAR(2048) · NOT NULL
- _index_ ix_report_templates_center_type(center_type)

## resident_accounts

- `id` · INTEGER · PK · NOT NULL
- `phone` · VARCHAR(20)
- `wechat_openid` · VARCHAR(64)
- `wechat_unionid` · VARCHAR(64) · NOT NULL
- `nickname` · VARCHAR(64) · NOT NULL
- `patient_id` · INTEGER · index · → patients.id
- `status` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- `last_login_at` · DATETIME
- _unique_ (phone)
- _unique_ (wechat_openid)
- _index_ ix_resident_accounts_patient_id(patient_id)
- _index_ uq_resident_account_patient(patient_id) UNIQUE

## resident_family_members

- `id` · INTEGER · PK · NOT NULL
- `account_id` · INTEGER · NOT NULL · index · → resident_accounts.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `relation` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (account_id, patient_id) uq_family_account_patient
- _index_ ix_resident_family_members_account_id(account_id)
- _index_ ix_resident_family_members_patient_id(patient_id)

## resources

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `resource_type` · VARCHAR(16) · NOT NULL · index
- `code` · VARCHAR(64) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `capacity` · INTEGER · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `location` · VARCHAR(256) · NOT NULL
- `contact` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `withdraw_reason` · VARCHAR(256) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (org_id, code) uq_resource_org_code
- _index_ ix_resources_code(code)
- _index_ ix_resources_org_id(org_id)
- _index_ ix_resources_resource_type(resource_type)
- _index_ ix_resources_status(status)

## role_change_logs

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `old_role` · VARCHAR(16) · NOT NULL
- `new_role` · VARCHAR(16) · NOT NULL
- `changed_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_role_change_logs_user_id(user_id)

## role_permissions

- `id` · INTEGER · PK · NOT NULL
- `role_id` · INTEGER · NOT NULL · index · → roles.id
- `permission_id` · INTEGER · NOT NULL · index · → permissions.id
- `created_at` · DATETIME · NOT NULL
- _unique_ (role_id, permission_id) uq_role_permission
- _index_ ix_role_permissions_permission_id(permission_id)
- _index_ ix_role_permissions_role_id(role_id)

## roles

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `description` · VARCHAR(256) · NOT NULL
- `builtin` · BOOLEAN · NOT NULL · index
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_roles_active(active)
- _index_ ix_roles_builtin(builtin)
- _index_ ix_roles_key(key) UNIQUE

## rule_definitions

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(48) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `domain` · VARCHAR(24) · NOT NULL · index
- `condition` · VARCHAR(512) · NOT NULL
- `message` · VARCHAR(256) · NOT NULL
- `severity` · VARCHAR(16) · NOT NULL · index
- `deduct_points` · INTEGER · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_rule_definitions_active(active)
- _index_ ix_rule_definitions_domain(domain)
- _index_ ix_rule_definitions_key(key) UNIQUE
- _index_ ix_rule_definitions_severity(severity)

## satisfaction_surveys

- `id` · INTEGER · PK · NOT NULL
- `target_type` · VARCHAR(16) · NOT NULL · index
- `target_id` · INTEGER · NOT NULL
- `patient_id` · INTEGER · NOT NULL · → patients.id
- `score` · INTEGER · NOT NULL
- `comment` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_satisfaction_surveys_target_type(target_type)

## scheduled_jobs

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `interval_seconds` · INTEGER · NOT NULL
- `enabled` · BOOLEAN · NOT NULL · index
- `last_run_at` · DATETIME
- `next_run_at` · DATETIME · index
- `last_status` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_scheduled_jobs_enabled(enabled)
- _index_ ix_scheduled_jobs_name(name) UNIQUE
- _index_ ix_scheduled_jobs_next_run_at(next_run_at)

## secondments

- `id` · INTEGER · PK · NOT NULL
- `employee_id` · INTEGER · NOT NULL · index · → employees.id
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `to_org_id` · INTEGER · NOT NULL · index · → organizations.id
- `start_date` · VARCHAR(10) · NOT NULL
- `end_date` · VARCHAR(10) · NOT NULL
- `assignment_type` · VARCHAR(16) · NOT NULL · index
- `position` · VARCHAR(64) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_secondments_assignment_type(assignment_type)
- _index_ ix_secondments_employee_id(employee_id)
- _index_ ix_secondments_to_org_id(to_org_id)

## service_blacklists

- `id` · INTEGER · PK · NOT NULL
- `domain` · VARCHAR(16) · NOT NULL · index
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `reason` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (domain, patient_id) uq_service_blacklist
- _index_ ix_service_blacklists_domain(domain)
- _index_ ix_service_blacklists_patient_id(patient_id)

## settlements

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `bill_type` · VARCHAR(16) · NOT NULL
- `admission_id` · INTEGER · index · → admissions.id
- `encounter_id` · INTEGER · → encounters.id
- `total_amount` · NUMERIC(14, 2) · NOT NULL
- `insurance_pay` · NUMERIC(14, 2) · NOT NULL
- `self_pay` · NUMERIC(14, 2) · NOT NULL
- `insurance_settlement_id` · INTEGER · → insurance_settlements.id
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_settlements_admission_id(admission_id)
- _index_ ix_settlements_org_id(org_id)
- _index_ ix_settlements_patient_id(patient_id)

## shift_handovers

- `id` · INTEGER · PK · NOT NULL
- `ward_id` · INTEGER · NOT NULL · index · → wards.id
- `shift` · VARCHAR(16) · NOT NULL · index
- `handover_date` · VARCHAR(10) · NOT NULL · index
- `from_staff` · VARCHAR(64) · NOT NULL
- `to_staff` · VARCHAR(64) · NOT NULL
- `patient_count` · INTEGER · NOT NULL
- `critical_count` · INTEGER · NOT NULL
- `content` · VARCHAR(2048) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_shift_handovers_handover_date(handover_date)
- _index_ ix_shift_handovers_shift(shift)
- _index_ ix_shift_handovers_ward_id(ward_id)

## simulation_attempts

- `id` · INTEGER · PK · NOT NULL
- `case_id` · INTEGER · NOT NULL · index · → simulation_cases.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `answers` · JSON · NOT NULL
- `score` · INTEGER · NOT NULL
- `passed` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_simulation_attempts_case_id(case_id)
- _index_ ix_simulation_attempts_passed(passed)
- _index_ ix_simulation_attempts_user_id(user_id)

## simulation_cases

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL · index
- `scenario` · VARCHAR(4096) · NOT NULL
- `decision_points` · JSON · NOT NULL
- `pass_score` · INTEGER · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_simulation_cases_active(active)
- _index_ ix_simulation_cases_category(category)

## sms_codes

- `id` · INTEGER · PK · NOT NULL
- `phone` · VARCHAR(20) · NOT NULL · index
- `purpose` · VARCHAR(16) · NOT NULL
- `code_hash` · VARCHAR(160) · NOT NULL
- `expires_at` · DATETIME · NOT NULL
- `attempts` · INTEGER · NOT NULL
- `consumed` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_sms_codes_created_at(created_at)
- _index_ ix_sms_codes_phone(phone)

## spd_assess_plans

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `level` · VARCHAR(16) · NOT NULL · index
- `program_codes` · JSON · NOT NULL
- `object_type` · VARCHAR(16) · NOT NULL
- `period_type` · VARCHAR(16) · NOT NULL
- `items` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_assess_plans_active(active)
- _index_ ix_spd_assess_plans_code(code) UNIQUE
- _index_ ix_spd_assess_plans_level(level)

## spd_assessments

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `scale_id` · INTEGER · NOT NULL · index · → spd_scales.id
- `scale_code` · VARCHAR(32) · NOT NULL · index
- `scale_version` · VARCHAR(16) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `answers` · JSON · NOT NULL
- `score` · FLOAT · NOT NULL
- `risk_level` · VARCHAR(16) · NOT NULL · index
- `advice` · VARCHAR(512) · NOT NULL
- `channel` · VARCHAR(16) · NOT NULL
- `operator_id` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_assessments_created_at(created_at)
- _index_ ix_spd_assessments_patient_id(patient_id)
- _index_ ix_spd_assessments_program_code(program_code)
- _index_ ix_spd_assessments_risk_level(risk_level)
- _index_ ix_spd_assessments_scale_code(scale_code)
- _index_ ix_spd_assessments_scale_id(scale_id)

## spd_call_tasks

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `phone` · VARCHAR(20) · NOT NULL · index
- `ref_type` · VARCHAR(24) · NOT NULL
- `ref_id` · INTEGER
- `status` · VARCHAR(16) · NOT NULL · index
- `operator_id` · INTEGER · → users.id
- `started_at` · DATETIME
- `duration_s` · INTEGER · NOT NULL
- `record_url` · VARCHAR(256) · NOT NULL
- `result` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_call_tasks_created_at(created_at)
- _index_ ix_spd_call_tasks_patient_id(patient_id)
- _index_ ix_spd_call_tasks_phone(phone)
- _index_ ix_spd_call_tasks_status(status)

## spd_candidates

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `source` · VARCHAR(16) · NOT NULL
- `screening_id` · INTEGER
- `org_id` · INTEGER · → organizations.id
- `team_id` · INTEGER · → spd_teams.id
- `assigned_user_id` · INTEGER · → users.id
- `risk_level` · VARCHAR(16) · NOT NULL
- `reason` · VARCHAR(256) · NOT NULL
- `matched_rules` · JSON · NOT NULL
- `claimed_at` · DATETIME
- `created_at` · DATETIME · NOT NULL · index
- _unique_ (patient_id, program_code) uq_spd_candidate_patient_program
- _index_ ix_spd_candidate_status_org(status, org_id)
- _index_ ix_spd_candidates_created_at(created_at)
- _index_ ix_spd_candidates_patient_id(patient_id)
- _index_ ix_spd_candidates_program_code(program_code)
- _index_ ix_spd_candidates_status(status)

## spd_case_report_tasks

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `dept` · VARCHAR(64) · NOT NULL
- `manager_user_id` · INTEGER · → users.id
- `assignee_ids` · JSON · NOT NULL
- `org_ids` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_case_report_tasks_active(active)
- _index_ ix_spd_case_report_tasks_code(code) UNIQUE
- _index_ ix_spd_case_report_tasks_program_code(program_code)

## spd_case_reports

- `id` · INTEGER · PK · NOT NULL
- `task_id` · INTEGER · index · → spd_case_report_tasks.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `report_type` · VARCHAR(16) · NOT NULL · index
- `content` · VARCHAR(1024) · NOT NULL
- `trigger_rule` · VARCHAR(128) · NOT NULL
- `reporter_id` · INTEGER · → users.id
- `org_id` · INTEGER · → organizations.id
- `status` · VARCHAR(16) · NOT NULL · index
- `handled_by` · INTEGER · → users.id
- `handle_note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- `handled_at` · DATETIME
- _index_ ix_spd_case_reports_created_at(created_at)
- _index_ ix_spd_case_reports_patient_id(patient_id)
- _index_ ix_spd_case_reports_program_code(program_code)
- _index_ ix_spd_case_reports_report_type(report_type)
- _index_ ix_spd_case_reports_status(status)
- _index_ ix_spd_case_reports_task_id(task_id)

## spd_centers

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `lead_org_id` · INTEGER · → organizations.id
- `lead_dept` · VARCHAR(64) · NOT NULL
- `leader_user_id` · INTEGER · → users.id
- `org_ids` · JSON · NOT NULL
- `team_ids` · JSON · NOT NULL
- `version` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_centers_code(code) UNIQUE
- _index_ ix_spd_centers_program_code(program_code)
- _index_ ix_spd_centers_status(status)

## spd_consult_messages

- `id` · INTEGER · PK · NOT NULL
- `consult_id` · INTEGER · NOT NULL · index · → spd_consults.id
- `sender` · VARCHAR(16) · NOT NULL
- `sender_id` · INTEGER
- `content` · VARCHAR(2048) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_consult_messages_consult_id(consult_id)
- _index_ ix_spd_consult_messages_created_at(created_at)

## spd_consults

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `doctor_id` · INTEGER · index · → users.id
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL · index
- `closed_at` · DATETIME
- _index_ ix_spd_consults_created_at(created_at)
- _index_ ix_spd_consults_doctor_id(doctor_id)
- _index_ ix_spd_consults_patient_id(patient_id)
- _index_ ix_spd_consults_program_code(program_code)
- _index_ ix_spd_consults_status(status)

## spd_data_sources

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `source_type` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · → organizations.id
- `endpoint` · VARCHAR(256) · NOT NULL
- `freq_minutes` · INTEGER · NOT NULL
- `scope` · VARCHAR(256) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `last_sync_at` · DATETIME
- `last_rows` · INTEGER · NOT NULL
- `last_latency_ms` · INTEGER · NOT NULL
- `success_rate` · FLOAT · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- _index_ ix_spd_data_sources_active(active)
- _index_ ix_spd_data_sources_code(code) UNIQUE
- _index_ ix_spd_data_sources_source_type(source_type)
- _index_ ix_spd_data_sources_status(status)

## spd_devices

- `id` · INTEGER · PK · NOT NULL
- `sn` · VARCHAR(64) · NOT NULL · index
- `device_type` · VARCHAR(16) · NOT NULL · index
- `model` · VARCHAR(64) · NOT NULL
- `org_id` · INTEGER · → organizations.id
- `bound_patient_id` · INTEGER · index · → patients.id
- `status` · VARCHAR(16) · NOT NULL · index
- `last_sync_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_devices_bound_patient_id(bound_patient_id)
- _index_ ix_spd_devices_device_type(device_type)
- _index_ ix_spd_devices_sn(sn) UNIQUE
- _index_ ix_spd_devices_status(status)

## spd_edu_materials

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `media_type` · VARCHAR(16) · NOT NULL
- `content` · VARCHAR(8192) · NOT NULL
- `media_url` · VARCHAR(256) · NOT NULL
- `dept` · VARCHAR(64) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_edu_materials_code(code) UNIQUE
- _index_ ix_spd_edu_materials_program_code(program_code)

## spd_edu_pushes

- `id` · INTEGER · PK · NOT NULL
- `material_id` · INTEGER · NOT NULL · index · → spd_edu_materials.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `channel` · VARCHAR(16) · NOT NULL
- `send_at` · VARCHAR(19) · NOT NULL
- `frequency` · VARCHAR(32) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `read_at` · DATETIME
- `operator_id` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_edu_pushes_created_at(created_at)
- _index_ ix_spd_edu_pushes_material_id(material_id)
- _index_ ix_spd_edu_pushes_patient_id(patient_id)
- _index_ ix_spd_edu_pushes_status(status)

## spd_enrollments

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `team_id` · INTEGER · index · → spd_teams.id
- `doctor_user_id` · INTEGER · → users.id
- `manager_user_id` · INTEGER · → users.id
- `village_doctor_id` · INTEGER · → users.id
- `stage` · VARCHAR(32) · NOT NULL · index
- `risk_level` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `source` · VARCHAR(16) · NOT NULL
- `migrated_from_id` · INTEGER
- `sign_date` · VARCHAR(10) · NOT NULL
- `consent_signed` · BOOLEAN · NOT NULL
- `consent_no` · VARCHAR(64) · NOT NULL
- `service_start` · VARCHAR(10) · NOT NULL
- `service_end` · VARCHAR(10) · NOT NULL
- `archived` · BOOLEAN · NOT NULL · index
- `habits` · JSON · NOT NULL
- `risk_factors` · JSON · NOT NULL
- `complications` · JSON · NOT NULL
- `tags` · JSON · NOT NULL
- `last_followup_at` · VARCHAR(10) · NOT NULL
- `next_followup_at` · VARCHAR(10) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_enroll_org_status(org_id, status)
- _index_ ix_spd_enrollments_archived(archived)
- _index_ ix_spd_enrollments_created_at(created_at)
- _index_ ix_spd_enrollments_next_followup_at(next_followup_at)
- _index_ ix_spd_enrollments_org_id(org_id)
- _index_ ix_spd_enrollments_patient_id(patient_id)
- _index_ ix_spd_enrollments_program_code(program_code)
- _index_ ix_spd_enrollments_risk_level(risk_level)
- _index_ ix_spd_enrollments_stage(stage)
- _index_ ix_spd_enrollments_status(status)
- _index_ ix_spd_enrollments_team_id(team_id)
- _index_ uq_spd_enroll_active_patient_program(patient_id, program_code) UNIQUE

## spd_followup_records

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `rule_id` · INTEGER · → spd_followup_rules.id
- `questionnaire_code` · VARCHAR(32) · NOT NULL
- `scene` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · → organizations.id
- `dept` · VARCHAR(64) · NOT NULL
- `planned_at` · VARCHAR(10) · NOT NULL · index
- `executed_at` · VARCHAR(10) · NOT NULL
- `channel` · VARCHAR(16) · NOT NULL
- `executor_id` · INTEGER · → users.id
- `answers` · JSON · NOT NULL
- `abnormal_level` · VARCHAR(16) · NOT NULL · index
- `result` · VARCHAR(512) · NOT NULL
- `evidence` · JSON · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_followup_records_abnormal_level(abnormal_level)
- _index_ ix_spd_followup_records_created_at(created_at)
- _index_ ix_spd_followup_records_patient_id(patient_id)
- _index_ ix_spd_followup_records_planned_at(planned_at)
- _index_ ix_spd_followup_records_program_code(program_code)
- _index_ ix_spd_followup_records_scene(scene)
- _index_ ix_spd_followup_records_status(status)
- _index_ ix_spd_fu_status_planned(status, planned_at)

## spd_followup_rules

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `scene` · VARCHAR(16) · NOT NULL · index
- `dept` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `diagnosis_keywords` · JSON · NOT NULL
- `surgery_keywords` · JSON · NOT NULL
- `order_keywords` · JSON · NOT NULL
- `points` · JSON · NOT NULL
- `questionnaire_code` · VARCHAR(32) · NOT NULL
- `executor_role` · VARCHAR(32) · NOT NULL
- `allow_depts` · JSON · NOT NULL
- `allow_roles` · JSON · NOT NULL
- `preset` · BOOLEAN · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_spd_followup_rules_active(active)
- _index_ ix_spd_followup_rules_code(code) UNIQUE
- _index_ ix_spd_followup_rules_program_code(program_code)
- _index_ ix_spd_followup_rules_scene(scene)

## spd_goods

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `points` · INTEGER · NOT NULL
- `stock` · INTEGER · NOT NULL
- `image_url` · VARCHAR(256) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_spd_goods_active(active)
- _index_ ix_spd_goods_code(code) UNIQUE

## spd_group_members

- `id` · INTEGER · PK · NOT NULL
- `group_id` · INTEGER · NOT NULL · index · → spd_groups.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `added_at` · DATETIME · NOT NULL
- _unique_ (group_id, patient_id) uq_spd_group_member
- _index_ ix_spd_group_members_group_id(group_id)
- _index_ ix_spd_group_members_patient_id(patient_id)

## spd_groups

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL · index
- `owner_user_id` · INTEGER · NOT NULL · index · → users.id
- `org_id` · INTEGER · → organizations.id
- `dept` · VARCHAR(64) · NOT NULL
- `scope` · VARCHAR(16) · NOT NULL · index
- `auto_rule` · JSON · NOT NULL
- `created_at` · DATETIME · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_spd_groups_name(name)
- _index_ ix_spd_groups_owner_user_id(owner_user_id)
- _index_ ix_spd_groups_scope(scope)

## spd_health_prescriptions

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `doctor_id` · INTEGER · → users.id
- `drug_advice` · VARCHAR(1024) · NOT NULL
- `rehab_advice` · VARCHAR(1024) · NOT NULL
- `life_advice` · VARCHAR(1024) · NOT NULL
- `target_note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_health_prescriptions_created_at(created_at)
- _index_ ix_spd_health_prescriptions_patient_id(patient_id)
- _index_ ix_spd_health_prescriptions_program_code(program_code)

## spd_indicators

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_codes` · JSON · NOT NULL
- `object_type` · VARCHAR(16) · NOT NULL · index
- `data_source` · VARCHAR(32) · NOT NULL
- `scope_expr` · VARCHAR(256) · NOT NULL
- `formula` · VARCHAR(256) · NOT NULL
- `score_rule` · JSON · NOT NULL
- `weight` · FLOAT · NOT NULL
- `target_value` · FLOAT
- `abnormal_rule` · VARCHAR(256) · NOT NULL
- `version` · VARCHAR(16) · NOT NULL
- `effective_from` · VARCHAR(10) · NOT NULL
- `effective_scope` · VARCHAR(64) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (code, version) uq_spd_indicator_code_version
- _index_ ix_spd_indicators_active(active)
- _index_ ix_spd_indicators_code(code)
- _index_ ix_spd_indicators_object_type(object_type)

## spd_intervention_templates

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `category` · VARCHAR(16) · NOT NULL
- `content` · VARCHAR(2048) · NOT NULL
- `measures` · VARCHAR(1024) · NOT NULL
- `frequency` · VARCHAR(32) · NOT NULL
- `cycle_days` · INTEGER · NOT NULL
- `auto_risk_level` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_spd_intervention_templates_code(code) UNIQUE
- _index_ ix_spd_intervention_templates_program_code(program_code)

## spd_interventions

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `enrollment_id` · INTEGER · index · → spd_enrollments.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `template_id` · INTEGER · → spd_intervention_templates.id
- `goal` · VARCHAR(256) · NOT NULL
- `content` · VARCHAR(2048) · NOT NULL
- `measures` · VARCHAR(1024) · NOT NULL
- `frequency` · VARCHAR(32) · NOT NULL
- `next_at` · VARCHAR(10) · NOT NULL · index
- `owner_id` · INTEGER · → users.id
- `status` · VARCHAR(16) · NOT NULL · index
- `read_at` · DATETIME
- `feedback` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_interventions_created_at(created_at)
- _index_ ix_spd_interventions_enrollment_id(enrollment_id)
- _index_ ix_spd_interventions_next_at(next_at)
- _index_ ix_spd_interventions_patient_id(patient_id)
- _index_ ix_spd_interventions_program_code(program_code)
- _index_ ix_spd_interventions_status(status)

## spd_lifecycle_events

- `id` · INTEGER · PK · NOT NULL
- `enrollment_id` · INTEGER · NOT NULL · index · → spd_enrollments.id
- `event` · VARCHAR(16) · NOT NULL · index
- `reason` · VARCHAR(256) · NOT NULL
- `detail` · VARCHAR(512) · NOT NULL
- `target_org_id` · INTEGER · → organizations.id
- `confirmed` · BOOLEAN · NOT NULL
- `confirmed_by` · INTEGER · → users.id
- `occurred_at` · VARCHAR(10) · NOT NULL
- `operator_id` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_lifecycle_events_enrollment_id(enrollment_id)
- _index_ ix_spd_lifecycle_events_event(event)

## spd_measurements

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `metric` · VARCHAR(32) · NOT NULL · index
- `value` · FLOAT · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `level` · VARCHAR(16) · NOT NULL · index
- `source` · VARCHAR(16) · NOT NULL
- `source_ref` · VARCHAR(64) · NOT NULL · index
- `device_sn` · VARCHAR(64) · NOT NULL
- `measured_at` · DATETIME · NOT NULL · index
- `operator_id` · INTEGER · → users.id
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_meas_patient_metric(patient_id, metric, measured_at)
- _index_ ix_spd_measurements_created_at(created_at)
- _index_ ix_spd_measurements_level(level)
- _index_ ix_spd_measurements_measured_at(measured_at)
- _index_ ix_spd_measurements_metric(metric)
- _index_ ix_spd_measurements_patient_id(patient_id)
- _index_ ix_spd_measurements_program_code(program_code)
- _index_ ix_spd_measurements_source_ref(source_ref)

## spd_package_bindings

- `id` · INTEGER · PK · NOT NULL
- `enrollment_id` · INTEGER · NOT NULL · index · → spd_enrollments.id
- `package_id` · INTEGER · NOT NULL · index · → spd_service_packages.id
- `items` · JSON · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `bound_at` · DATETIME · NOT NULL
- `unbound_at` · DATETIME
- `period_end` · VARCHAR(10) · NOT NULL
- _index_ ix_spd_package_bindings_enrollment_id(enrollment_id)
- _index_ ix_spd_package_bindings_package_id(package_id)
- _index_ ix_spd_package_bindings_status(status)

## spd_package_usages

- `id` · INTEGER · PK · NOT NULL
- `binding_id` · INTEGER · NOT NULL · index · → spd_package_bindings.id
- `item_code` · VARCHAR(32) · NOT NULL
- `item_name` · VARCHAR(64) · NOT NULL
- `qty` · INTEGER · NOT NULL
- `price` · NUMERIC(14, 2) · NOT NULL
- `operator_id` · INTEGER · → users.id
- `note` · VARCHAR(256) · NOT NULL
- `used_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_package_usages_binding_id(binding_id)
- _index_ ix_spd_package_usages_used_at(used_at)

## spd_path_instances

- `id` · INTEGER · PK · NOT NULL
- `enrollment_id` · INTEGER · NOT NULL · index · → spd_enrollments.id
- `template_id` · INTEGER · NOT NULL · index · → spd_path_templates.id
- `template_code` · VARCHAR(32) · NOT NULL
- `current_node_key` · VARCHAR(32) · NOT NULL · index
- `current_stage` · VARCHAR(32) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `overrides` · JSON · NOT NULL
- `progress` · INTEGER · NOT NULL
- `started_at` · DATETIME · NOT NULL
- `finished_at` · DATETIME
- `owner_user_id` · INTEGER · → users.id
- _index_ ix_spd_path_instances_current_node_key(current_node_key)
- _index_ ix_spd_path_instances_enrollment_id(enrollment_id)
- _index_ ix_spd_path_instances_status(status)
- _index_ ix_spd_path_instances_template_id(template_id)

## spd_path_nodes

- `id` · INTEGER · PK · NOT NULL
- `template_id` · INTEGER · NOT NULL · index · → spd_path_templates.id
- `key` · VARCHAR(32) · NOT NULL
- `name` · VARCHAR(64) · NOT NULL
- `stage` · VARCHAR(32) · NOT NULL
- `seq` · INTEGER · NOT NULL
- `dept` · VARCHAR(64) · NOT NULL
- `exec_role` · VARCHAR(32) · NOT NULL
- `service_type` · VARCHAR(16) · NOT NULL
- `enter_condition` · JSON · NOT NULL
- `complete_condition` · JSON · NOT NULL
- `next_key` · VARCHAR(32) · NOT NULL
- `due_days` · INTEGER · NOT NULL
- `timeout_action` · VARCHAR(16) · NOT NULL
- `require_form` · BOOLEAN · NOT NULL
- `require_evidence` · BOOLEAN · NOT NULL
- `form_code` · VARCHAR(32) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- _unique_ (template_id, key) uq_spd_node_key
- _index_ ix_spd_path_nodes_template_id(template_id)

## spd_path_templates

- `id` · INTEGER · PK · NOT NULL
- `program_id` · INTEGER · NOT NULL · index · → spd_programs.id
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `scene` · VARCHAR(16) · NOT NULL · index
- `risk_level` · VARCHAR(16) · NOT NULL
- `version` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `copied_from_id` · INTEGER
- `scope` · VARCHAR(16) · NOT NULL
- `org_id` · INTEGER · → organizations.id
- `team_id` · INTEGER
- `description` · VARCHAR(512) · NOT NULL
- `created_by` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (code, version) uq_spd_path_code_version
- _index_ ix_spd_path_templates_code(code)
- _index_ ix_spd_path_templates_program_id(program_id)
- _index_ ix_spd_path_templates_scene(scene)
- _index_ ix_spd_path_templates_status(status)

## spd_point_accounts

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `org_id` · INTEGER · → organizations.id
- `balance` · INTEGER · NOT NULL
- `earned` · INTEGER · NOT NULL
- `used` · INTEGER · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_spd_point_accounts_user_id(user_id) UNIQUE

## spd_point_records

- `id` · INTEGER · PK · NOT NULL
- `account_id` · INTEGER · NOT NULL · index · → spd_point_accounts.id
- `rule_code` · VARCHAR(32) · NOT NULL
- `direction` · VARCHAR(8) · NOT NULL · index
- `points` · INTEGER · NOT NULL
- `balance_after` · INTEGER · NOT NULL
- `ref_type` · VARCHAR(24) · NOT NULL
- `ref_id` · INTEGER
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_point_records_account_id(account_id)
- _index_ ix_spd_point_records_created_at(created_at)
- _index_ ix_spd_point_records_direction(direction)

## spd_point_rules

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `event` · VARCHAR(24) · NOT NULL · index
- `points` · INTEGER · NOT NULL
- `daily_limit` · INTEGER · NOT NULL
- `condition` · VARCHAR(256) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_spd_point_rules_active(active)
- _index_ ix_spd_point_rules_code(code) UNIQUE
- _index_ ix_spd_point_rules_event(event)

## spd_program_versions

- `id` · INTEGER · PK · NOT NULL
- `program_id` · INTEGER · NOT NULL · index · → spd_programs.id
- `version` · VARCHAR(16) · NOT NULL
- `snapshot` · JSON · NOT NULL
- `changed_by` · VARCHAR(64) · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_program_versions_program_id(program_id)

## spd_programs

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL · index
- `lead_org_id` · INTEGER · → organizations.id
- `lead_dept` · VARCHAR(64) · NOT NULL
- `description` · VARCHAR(512) · NOT NULL
- `include_rules` · JSON · NOT NULL
- `exclude_rules` · JSON · NOT NULL
- `stages` · JSON · NOT NULL
- `milestones` · JSON · NOT NULL
- `version` · VARCHAR(16) · NOT NULL
- `effective_from` · VARCHAR(10) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_programs_active(active)
- _index_ ix_spd_programs_category(category)
- _index_ ix_spd_programs_code(code) UNIQUE

## spd_qc_samples

- `id` · INTEGER · PK · NOT NULL
- `record_id` · INTEGER · NOT NULL · index · → spd_followup_records.id
- `batch` · VARCHAR(32) · NOT NULL · index
- `dept` · VARCHAR(64) · NOT NULL
- `sampler_id` · INTEGER · → users.id
- `method` · VARCHAR(16) · NOT NULL
- `result` · VARCHAR(16) · NOT NULL · index
- `note` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_qc_samples_batch(batch)
- _index_ ix_spd_qc_samples_created_at(created_at)
- _index_ ix_spd_qc_samples_record_id(record_id)
- _index_ ix_spd_qc_samples_result(result)

## spd_questionnaires

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `scene` · VARCHAR(16) · NOT NULL
- `items` · JSON · NOT NULL
- `abnormal_rules` · JSON · NOT NULL
- `track_dept` · VARCHAR(64) · NOT NULL
- `handle_role` · VARCHAR(32) · NOT NULL
- `preset` · BOOLEAN · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_spd_questionnaires_active(active)
- _index_ ix_spd_questionnaires_code(code) UNIQUE

## spd_recalls

- `id` · INTEGER · PK · NOT NULL
- `enrollment_id` · INTEGER · NOT NULL · index · → spd_enrollments.id
- `reason` · VARCHAR(256) · NOT NULL
- `contacts` · JSON · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `result` · VARCHAR(256) · NOT NULL
- `operator_id` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- `closed_at` · DATETIME
- _index_ ix_spd_recalls_enrollment_id(enrollment_id)
- _index_ ix_spd_recalls_status(status)

## spd_redeems

- `id` · INTEGER · PK · NOT NULL
- `account_id` · INTEGER · NOT NULL · index · → spd_point_accounts.id
- `goods_id` · INTEGER · NOT NULL · index · → spd_goods.id
- `points` · INTEGER · NOT NULL
- `verify_code` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `verified_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- `verified_at` · DATETIME
- _index_ ix_spd_redeems_account_id(account_id)
- _index_ ix_spd_redeems_created_at(created_at)
- _index_ ix_spd_redeems_goods_id(goods_id)
- _index_ ix_spd_redeems_status(status)
- _index_ ix_spd_redeems_verify_code(verify_code)

## spd_referral_cases

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `enrollment_id` · INTEGER · index · → spd_enrollments.id
- `direction` · VARCHAR(8) · NOT NULL · index
- `initiator_org_id` · INTEGER · NOT NULL · index · → organizations.id
- `initiator_id` · INTEGER · → users.id
- `current_org_id` · INTEGER · index · → organizations.id
- `current_level` · VARCHAR(16) · NOT NULL · index
- `target_org_id` · INTEGER · → organizations.id
- `status` · VARCHAR(24) · NOT NULL · index
- `reason` · VARCHAR(512) · NOT NULL
- `trigger_rule_code` · VARCHAR(32) · NOT NULL
- `trigger_evidence` · JSON · NOT NULL
- `materials` · JSON · NOT NULL
- `effective_visit` · BOOLEAN · NOT NULL · index
- `stable_for_down` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- `closed_at` · DATETIME
- _index_ ix_spd_ref_status_org(status, current_org_id)
- _index_ ix_spd_referral_cases_created_at(created_at)
- _index_ ix_spd_referral_cases_current_level(current_level)
- _index_ ix_spd_referral_cases_current_org_id(current_org_id)
- _index_ ix_spd_referral_cases_direction(direction)
- _index_ ix_spd_referral_cases_effective_visit(effective_visit)
- _index_ ix_spd_referral_cases_enrollment_id(enrollment_id)
- _index_ ix_spd_referral_cases_initiator_org_id(initiator_org_id)
- _index_ ix_spd_referral_cases_patient_id(patient_id)
- _index_ ix_spd_referral_cases_program_code(program_code)
- _index_ ix_spd_referral_cases_status(status)

## spd_referral_rules

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `scene` · VARCHAR(16) · NOT NULL
- `conditions` · JSON · NOT NULL
- `notify_role` · VARCHAR(32) · NOT NULL
- `handle_level` · VARCHAR(16) · NOT NULL
- `target_org_id` · INTEGER · → organizations.id
- `auto_task` · BOOLEAN · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- _index_ ix_spd_referral_rules_active(active)
- _index_ ix_spd_referral_rules_code(code) UNIQUE
- _index_ ix_spd_referral_rules_program_code(program_code)

## spd_referral_steps

- `id` · INTEGER · PK · NOT NULL
- `case_id` · INTEGER · NOT NULL · index · → spd_referral_cases.id
- `step` · VARCHAR(24) · NOT NULL
- `action` · VARCHAR(16) · NOT NULL
- `actor_id` · INTEGER · → users.id
- `org_id` · INTEGER · → organizations.id
- `opinion` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_referral_steps_case_id(case_id)
- _index_ ix_spd_referral_steps_created_at(created_at)

## spd_report_instances

- `id` · INTEGER · PK · NOT NULL
- `task_id` · INTEGER · → spd_report_tasks.id
- `template_code` · VARCHAR(32) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `period_label` · VARCHAR(32) · NOT NULL · index
- `scope_level` · VARCHAR(16) · NOT NULL
- `org_id` · INTEGER · → organizations.id
- `content` · JSON · NOT NULL
- `subscriber_ids` · JSON · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_report_instances_created_at(created_at)
- _index_ ix_spd_report_instances_period_label(period_label)
- _index_ ix_spd_report_instances_template_code(template_code)

## spd_report_tasks

- `id` · INTEGER · PK · NOT NULL
- `template_id` · INTEGER · NOT NULL · index · → spd_report_templates.id
- `name` · VARCHAR(64) · NOT NULL
- `frequency` · VARCHAR(16) · NOT NULL
- `push_time` · VARCHAR(5) · NOT NULL
- `subscriber_ids` · JSON · NOT NULL
- `org_ids` · JSON · NOT NULL
- `valid_from` · VARCHAR(10) · NOT NULL
- `valid_to` · VARCHAR(10) · NOT NULL
- `priority` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `last_run_at` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_report_tasks_status(status)
- _index_ ix_spd_report_tasks_template_id(template_id)

## spd_report_templates

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `period` · VARCHAR(16) · NOT NULL · index
- `scope_level` · VARCHAR(16) · NOT NULL · index
- `sections` · JSON · NOT NULL
- `variables` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_report_templates_active(active)
- _index_ ix_spd_report_templates_code(code) UNIQUE
- _index_ ix_spd_report_templates_period(period)
- _index_ ix_spd_report_templates_scope_level(scope_level)

## spd_revisits

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `plan_date` · VARCHAR(10) · NOT NULL · index
- `dept` · VARCHAR(64) · NOT NULL
- `doctor_user_id` · INTEGER · → users.id
- `items` · VARCHAR(512) · NOT NULL
- `source` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `remind_status` · VARCHAR(16) · NOT NULL
- `actual_date` · VARCHAR(10) · NOT NULL
- `log` · JSON · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_revisits_patient_id(patient_id)
- _index_ ix_spd_revisits_plan_date(plan_date)
- _index_ ix_spd_revisits_program_code(program_code)
- _index_ ix_spd_revisits_source(source)
- _index_ ix_spd_revisits_status(status)

## spd_scales

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `category` · VARCHAR(16) · NOT NULL · index
- `program_code` · VARCHAR(32) · NOT NULL · index
- `version` · VARCHAR(16) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `items` · JSON · NOT NULL
- `scoring` · JSON · NOT NULL
- `qr_token` · VARCHAR(32) · NOT NULL · index
- `owner_team_id` · INTEGER
- `created_at` · DATETIME · NOT NULL
- _unique_ (code, version) uq_spd_scale_code_version
- _index_ ix_spd_scales_category(category)
- _index_ ix_spd_scales_code(code)
- _index_ ix_spd_scales_program_code(program_code)
- _index_ ix_spd_scales_qr_token(qr_token)
- _index_ ix_spd_scales_status(status)

## spd_scores

- `id` · INTEGER · PK · NOT NULL
- `plan_id` · INTEGER · NOT NULL · index · → spd_assess_plans.id
- `period` · VARCHAR(16) · NOT NULL · index
- `object_type` · VARCHAR(16) · NOT NULL
- `object_id` · INTEGER · NOT NULL · index
- `object_name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `total_score` · FLOAT · NOT NULL
- `rank` · INTEGER · NOT NULL
- `detail` · JSON · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _unique_ (plan_id, period, object_type, object_id) uq_spd_score
- _index_ ix_spd_scores_created_at(created_at)
- _index_ ix_spd_scores_object_id(object_id)
- _index_ ix_spd_scores_period(period)
- _index_ ix_spd_scores_plan_id(plan_id)
- _index_ ix_spd_scores_program_code(program_code)

## spd_screenings

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `source` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · → organizations.id
- `operator_id` · INTEGER · → users.id
- `scale_code` · VARCHAR(32) · NOT NULL
- `answers` · JSON · NOT NULL
- `score` · FLOAT · NOT NULL
- `risk_level` · VARCHAR(16) · NOT NULL · index
- `result` · VARCHAR(16) · NOT NULL · index
- `advice` · VARCHAR(512) · NOT NULL
- `reviewed` · BOOLEAN · NOT NULL · index
- `review_result` · VARCHAR(16) · NOT NULL
- `review_note` · VARCHAR(256) · NOT NULL
- `reviewer_id` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_spd_screenings_created_at(created_at)
- _index_ ix_spd_screenings_patient_id(patient_id)
- _index_ ix_spd_screenings_program_code(program_code)
- _index_ ix_spd_screenings_result(result)
- _index_ ix_spd_screenings_reviewed(reviewed)
- _index_ ix_spd_screenings_risk_level(risk_level)
- _index_ ix_spd_screenings_source(source)

## spd_service_applies

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `program_code` · VARCHAR(32) · NOT NULL · index
- `screening_id` · INTEGER
- `note` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `handled_by` · INTEGER · → users.id
- `handle_note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- `handled_at` · DATETIME
- _index_ ix_spd_service_applies_created_at(created_at)
- _index_ ix_spd_service_applies_patient_id(patient_id)
- _index_ ix_spd_service_applies_program_code(program_code)
- _index_ ix_spd_service_applies_status(status)

## spd_service_packages

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `price` · NUMERIC(14, 2) · NOT NULL
- `period_days` · INTEGER · NOT NULL
- `items` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_spd_service_packages_code(code) UNIQUE
- _index_ ix_spd_service_packages_program_code(program_code)

## spd_signins

- `id` · INTEGER · PK · NOT NULL
- `account_id` · INTEGER · NOT NULL · index · → spd_point_accounts.id
- `day` · VARCHAR(10) · NOT NULL
- `points` · INTEGER · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (account_id, day) uq_spd_signin_day
- _index_ ix_spd_signins_account_id(account_id)

## spd_sync_logs

- `id` · INTEGER · PK · NOT NULL
- `source_id` · INTEGER · NOT NULL · index · → spd_data_sources.id
- `started_at` · DATETIME · NOT NULL · index
- `rows` · INTEGER · NOT NULL
- `latency_ms` · INTEGER · NOT NULL
- `success` · BOOLEAN · NOT NULL
- `message` · VARCHAR(256) · NOT NULL
- _index_ ix_spd_sync_logs_source_id(source_id)
- _index_ ix_spd_sync_logs_started_at(started_at)

## spd_tags

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(64) · NOT NULL
- `category` · VARCHAR(32) · NOT NULL
- `color` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _index_ ix_spd_tags_code(code) UNIQUE

## spd_targets

- `id` · INTEGER · PK · NOT NULL
- `program_id` · INTEGER · NOT NULL · index · → spd_programs.id
- `stage` · VARCHAR(32) · NOT NULL · index
- `metric` · VARCHAR(32) · NOT NULL
- `metric_name` · VARCHAR(64) · NOT NULL
- `kind` · VARCHAR(16) · NOT NULL
- `target_low` · FLOAT
- `target_high` · FLOAT
- `unit` · VARCHAR(16) · NOT NULL
- `qualitative` · VARCHAR(128) · NOT NULL
- `risk_level` · VARCHAR(16) · NOT NULL
- `followup_interval_days` · INTEGER · NOT NULL
- `form_code` · VARCHAR(32) · NOT NULL
- `edu_code` · VARCHAR(32) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _unique_ (program_id, stage, metric) uq_spd_target_stage_metric
- _index_ ix_spd_targets_program_id(program_id)
- _index_ ix_spd_targets_stage(stage)

## spd_tasks

- `id` · INTEGER · PK · NOT NULL
- `program_code` · VARCHAR(32) · NOT NULL · index
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `enrollment_id` · INTEGER · index · → spd_enrollments.id
- `instance_id` · INTEGER · index · → spd_path_instances.id
- `node_key` · VARCHAR(32) · NOT NULL
- `task_type` · VARCHAR(16) · NOT NULL · index
- `title` · VARCHAR(128) · NOT NULL
- `org_id` · INTEGER · → organizations.id
- `team_id` · INTEGER · → spd_teams.id
- `assignee_id` · INTEGER · → users.id
- `exec_role` · VARCHAR(32) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `priority` · INTEGER · NOT NULL · index
- `due_date` · VARCHAR(10) · NOT NULL · index
- `form_code` · VARCHAR(32) · NOT NULL
- `require_evidence` · BOOLEAN · NOT NULL
- `form` · JSON · NOT NULL
- `result` · JSON · NOT NULL
- `evidence` · JSON · NOT NULL
- `urged_count` · INTEGER · NOT NULL
- `escalated` · BOOLEAN · NOT NULL · index
- `transferred_from` · INTEGER
- `reviewer_id` · INTEGER · → users.id
- `review_note` · VARCHAR(256) · NOT NULL
- `source` · VARCHAR(16) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- `finished_at` · DATETIME
- _index_ ix_spd_task_assignee_status(assignee_id, status)
- _index_ ix_spd_task_org_due(org_id, due_date)
- _index_ ix_spd_tasks_created_at(created_at)
- _index_ ix_spd_tasks_due_date(due_date)
- _index_ ix_spd_tasks_enrollment_id(enrollment_id)
- _index_ ix_spd_tasks_escalated(escalated)
- _index_ ix_spd_tasks_instance_id(instance_id)
- _index_ ix_spd_tasks_patient_id(patient_id)
- _index_ ix_spd_tasks_priority(priority)
- _index_ ix_spd_tasks_program_code(program_code)
- _index_ ix_spd_tasks_status(status)
- _index_ ix_spd_tasks_task_type(task_type)

## spd_team_members

- `id` · INTEGER · PK · NOT NULL
- `team_id` · INTEGER · NOT NULL · index · → spd_teams.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `member_role` · VARCHAR(16) · NOT NULL · index
- `program_codes` · JSON · NOT NULL
- `stage_scope` · VARCHAR(64) · NOT NULL
- `patient_scope` · VARCHAR(16) · NOT NULL
- `can_view` · BOOLEAN · NOT NULL
- `can_followup` · BOOLEAN · NOT NULL
- `can_referral` · BOOLEAN · NOT NULL
- `can_audit` · BOOLEAN · NOT NULL
- `can_assess` · BOOLEAN · NOT NULL
- `active` · BOOLEAN · NOT NULL
- _unique_ (team_id, user_id) uq_spd_team_member
- _index_ ix_spd_team_members_member_role(member_role)
- _index_ ix_spd_team_members_team_id(team_id)
- _index_ ix_spd_team_members_user_id(user_id)

## spd_teams

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(64) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `level` · VARCHAR(16) · NOT NULL · index
- `program_codes` · JSON · NOT NULL
- `leader_user_id` · INTEGER · → users.id
- `dept` · VARCHAR(64) · NOT NULL
- `service_area` · VARCHAR(256) · NOT NULL
- `data_scope` · VARCHAR(16) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_teams_level(level)
- _index_ ix_spd_teams_name(name)
- _index_ ix_spd_teams_org_id(org_id)

## spd_village_doctors

- `id` · INTEGER · PK · NOT NULL
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `township` · VARCHAR(64) · NOT NULL · index
- `village` · VARCHAR(64) · NOT NULL · index
- `license_no` · VARCHAR(64) · NOT NULL
- `license_valid_to` · VARCHAR(10) · NOT NULL
- `phone` · VARCHAR(20) · NOT NULL
- `bind_token` · VARCHAR(32) · NOT NULL · index
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_spd_village_doctors_bind_token(bind_token)
- _index_ ix_spd_village_doctors_org_id(org_id)
- _index_ ix_spd_village_doctors_township(township)
- _index_ ix_spd_village_doctors_user_id(user_id) UNIQUE
- _index_ ix_spd_village_doctors_village(village)

## special_disease_apps

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `disease_name` · VARCHAR(128) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_special_disease_apps_patient_id(patient_id)

## staff_contracts

- `id` · INTEGER · PK · NOT NULL
- `employee_id` · INTEGER · NOT NULL · index · → employees.id
- `contract_no` · VARCHAR(64) · NOT NULL
- `start_date` · VARCHAR(10) · NOT NULL
- `end_date` · VARCHAR(10) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (contract_no)
- _index_ ix_staff_contracts_employee_id(employee_id)
- _index_ ix_staff_contracts_status(status)

## sterilization_batches

- `id` · INTEGER · PK · NOT NULL
- `batch_no` · VARCHAR(32) · NOT NULL · index
- `center_org_id` · INTEGER · NOT NULL · → organizations.id
- `item_name` · VARCHAR(128) · NOT NULL
- `quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `dispatched_to_org_id` · INTEGER · → organizations.id
- `created_at` · DATETIME · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_sterilization_batches_batch_no(batch_no) UNIQUE
- _index_ ix_sterilization_batches_status(status)

## stock_takes

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `drug_code` · VARCHAR(64) · NOT NULL
- `book_qty` · INTEGER · NOT NULL
- `actual_qty` · INTEGER · NOT NULL
- `diff` · INTEGER · NOT NULL
- `note` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_stock_takes_org_id(org_id)

## stock_transfers

- `id` · INTEGER · PK · NOT NULL
- `drug_code` · VARCHAR(64) · NOT NULL
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `to_org_id` · INTEGER · NOT NULL · → organizations.id
- `quantity` · INTEGER · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL

## suppliers

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(128) · NOT NULL
- `contact` · VARCHAR(64) · NOT NULL
- `license_no` · VARCHAR(64) · NOT NULL
- `active` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (name)

## surgery_records

- `id` · INTEGER · PK · NOT NULL
- `request_id` · INTEGER · NOT NULL · index · → surgery_requests.id
- `actual_surgery_name` · VARCHAR(256) · NOT NULL
- `surgeon_name` · VARCHAR(64) · NOT NULL
- `assistants` · VARCHAR(256) · NOT NULL
- `anesthetist_name` · VARCHAR(64) · NOT NULL
- `anesthesia_type` · VARCHAR(16) · NOT NULL
- `incision_level` · VARCHAR(4) · NOT NULL
- `start_at` · VARCHAR(16) · NOT NULL
- `end_at` · VARCHAR(16) · NOT NULL
- `blood_loss_ml` · INTEGER · NOT NULL
- `findings` · VARCHAR(2048) · NOT NULL
- `procedure` · VARCHAR(4096) · NOT NULL
- `complications` · VARCHAR(1024) · NOT NULL
- `outcome` · VARCHAR(16) · NOT NULL
- `preop_diagnosis` · VARCHAR(256) · NOT NULL
- `postop_diagnosis` · VARCHAR(256) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_surgery_records_created_at(created_at)
- _index_ ix_surgery_records_request_id(request_id) UNIQUE

## surgery_requests

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · NOT NULL · index · → admissions.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `surgery_name` · VARCHAR(256) · NOT NULL
- `surgery_code` · VARCHAR(32) · NOT NULL
- `incision_level` · VARCHAR(4) · NOT NULL
- `anesthesia_type` · VARCHAR(16) · NOT NULL
- `surgeon_name` · VARCHAR(64) · NOT NULL
- `urgency` · VARCHAR(16) · NOT NULL · index
- `unplanned_return` · BOOLEAN · NOT NULL · index
- `planned_date` · VARCHAR(10) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `approved_by` · INTEGER · → users.id
- `approved_at` · DATETIME
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_surgery_requests_admission_id(admission_id)
- _index_ ix_surgery_requests_created_at(created_at)
- _index_ ix_surgery_requests_org_id(org_id)
- _index_ ix_surgery_requests_patient_id(patient_id)
- _index_ ix_surgery_requests_status(status)
- _index_ ix_surgery_requests_unplanned_return(unplanned_return)
- _index_ ix_surgery_requests_urgency(urgency)

## surgery_schedules

- `id` · INTEGER · PK · NOT NULL
- `request_id` · INTEGER · NOT NULL · index · → surgery_requests.id
- `room_id` · INTEGER · NOT NULL · index · → operating_rooms.id
- `scheduled_date` · VARCHAR(10) · NOT NULL · index
- `start_time` · VARCHAR(5) · NOT NULL
- `end_time` · VARCHAR(5) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _unique_ (room_id, scheduled_date, start_time) uq_schedule_room_slot
- _index_ ix_surgery_schedules_request_id(request_id) UNIQUE
- _index_ ix_surgery_schedules_room_id(room_id)
- _index_ ix_surgery_schedules_scheduled_date(scheduled_date)

## syndrome_monitors

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `syndrome` · VARCHAR(16) · NOT NULL · index
- `case_count` · INTEGER · NOT NULL
- `threshold` · INTEGER · NOT NULL
- `record_date` · VARCHAR(10) · NOT NULL · index
- `note` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (org_id, syndrome, record_date) uq_syndrome_daily
- _index_ ix_syndrome_monitors_org_id(org_id)
- _index_ ix_syndrome_monitors_record_date(record_date)
- _index_ ix_syndrome_monitors_syndrome(syndrome)

## system_params

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(64) · NOT NULL · index
- `value` · VARCHAR(256) · NOT NULL
- `description` · VARCHAR(256) · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_system_params_key(key) UNIQUE

## tcm_dispense_orders

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `from_org_id` · INTEGER · NOT NULL · → organizations.id
- `herbs` · VARCHAR(1024) · NOT NULL
- `doses` · INTEGER · NOT NULL
- `decoct` · BOOLEAN · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_tcm_dispense_orders_patient_id(patient_id)
- _index_ ix_tcm_dispense_orders_status(status)

## tcm_formulas

- `id` · INTEGER · PK · NOT NULL
- `code` · VARCHAR(32) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `dosage_form` · VARCHAR(16) · NOT NULL · index
- `composition` · VARCHAR(1024) · NOT NULL
- `process` · VARCHAR(1024) · NOT NULL
- `indication` · VARCHAR(512) · NOT NULL
- `shelf_life_months` · INTEGER · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_tcm_formulas_active(active)
- _index_ ix_tcm_formulas_code(code) UNIQUE
- _index_ ix_tcm_formulas_dosage_form(dosage_form)

## tcm_master_cases

- `id` · INTEGER · PK · NOT NULL
- `master_name` · VARCHAR(64) · NOT NULL · index
- `successor_name` · VARCHAR(64) · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `disease` · VARCHAR(128) · NOT NULL · index
- `syndrome` · VARCHAR(128) · NOT NULL · index
- `four_exams` · VARCHAR(2048) · NOT NULL
- `treatment_method` · VARCHAR(512) · NOT NULL
- `prescription` · VARCHAR(1024) · NOT NULL
- `commentary` · VARCHAR(2048) · NOT NULL
- `visit_date` · VARCHAR(10) · NOT NULL
- `published` · BOOLEAN · NOT NULL · index
- `created_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_tcm_master_cases_disease(disease)
- _index_ ix_tcm_master_cases_master_name(master_name)
- _index_ ix_tcm_master_cases_published(published)
- _index_ ix_tcm_master_cases_syndrome(syndrome)

## tcm_preparation_batches

- `id` · INTEGER · PK · NOT NULL
- `formula_id` · INTEGER · NOT NULL · index · → tcm_formulas.id
- `batch_no` · VARCHAR(32) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `quantity` · INTEGER · NOT NULL
- `unit` · VARCHAR(16) · NOT NULL
- `produced_date` · VARCHAR(10) · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_tcm_preparation_batches_batch_no(batch_no) UNIQUE
- _index_ ix_tcm_preparation_batches_expire_date(expire_date)
- _index_ ix_tcm_preparation_batches_formula_id(formula_id)
- _index_ ix_tcm_preparation_batches_org_id(org_id)
- _index_ ix_tcm_preparation_batches_status(status)

## tcm_techniques

- `id` · INTEGER · PK · NOT NULL
- `name` · VARCHAR(128) · NOT NULL
- `category` · VARCHAR(64) · NOT NULL
- `indication` · VARCHAR(512) · NOT NULL
- `description` · VARCHAR(1024) · NOT NULL
- _unique_ (name)

## training_assessments

- `id` · INTEGER · PK · NOT NULL
- `plan_id` · INTEGER · NOT NULL · index · → training_plans.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `score` · FLOAT · NOT NULL
- `passed` · BOOLEAN · NOT NULL · index
- `comment` · VARCHAR(512) · NOT NULL
- `assessor` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (plan_id, user_id) uq_assess_plan_user
- _index_ ix_training_assessments_passed(passed)
- _index_ ix_training_assessments_plan_id(plan_id)
- _index_ ix_training_assessments_user_id(user_id)

## training_enrollments

- `id` · INTEGER · PK · NOT NULL
- `plan_id` · INTEGER · NOT NULL · index · → training_plans.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `status` · VARCHAR(16) · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _unique_ (plan_id, user_id) uq_enroll_plan_user
- _index_ ix_training_enrollments_plan_id(plan_id)
- _index_ ix_training_enrollments_status(status)
- _index_ ix_training_enrollments_user_id(user_id)

## training_plans

- `id` · INTEGER · PK · NOT NULL
- `title` · VARCHAR(256) · NOT NULL
- `technique_id` · INTEGER · → tcm_techniques.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `plan_date` · VARCHAR(10) · NOT NULL
- `capacity` · INTEGER · NOT NULL
- `enrolled_count` · INTEGER · NOT NULL
- `trainer` · VARCHAR(64) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_training_plans_org_id(org_id)
- _index_ ix_training_plans_status(status)

## training_records

- `id` · INTEGER · PK · NOT NULL
- `course_id` · INTEGER · NOT NULL · index · → courses.id
- `user_id` · INTEGER · NOT NULL · index · → users.id
- `score` · FLOAT · NOT NULL
- `passed` · BOOLEAN · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (course_id, user_id) uq_training_course_user
- _index_ ix_training_records_course_id(course_id)
- _index_ ix_training_records_user_id(user_id)

## transfusion_requests

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `blood_type` · VARCHAR(4) · NOT NULL
- `component` · VARCHAR(16) · NOT NULL
- `quantity_ml` · INTEGER · NOT NULL
- `reason` · VARCHAR(512) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `requested_by` · INTEGER · NOT NULL · → users.id
- `approved_by` · INTEGER · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_transfusion_requests_patient_id(patient_id)
- _index_ ix_transfusion_requests_status(status)

## treatment_records

- `id` · INTEGER · PK · NOT NULL
- `encounter_id` · INTEGER · NOT NULL · index · → encounters.id
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `treatment_name` · VARCHAR(128) · NOT NULL
- `treatment_code` · VARCHAR(64) · NOT NULL
- `site` · VARCHAR(64) · NOT NULL
- `dose` · VARCHAR(64) · NOT NULL
- `executor_name` · VARCHAR(64) · NOT NULL
- `performed_at` · VARCHAR(16) · NOT NULL
- `reaction` · VARCHAR(256) · NOT NULL
- `note` · VARCHAR(512) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_treatment_records_created_at(created_at)
- _index_ ix_treatment_records_encounter_id(encounter_id)
- _index_ ix_treatment_records_org_id(org_id)
- _index_ ix_treatment_records_patient_id(patient_id)

## users

- `id` · INTEGER · PK · NOT NULL
- `username` · VARCHAR(64) · NOT NULL · index
- `password_hash` · VARCHAR(200) · NOT NULL
- `full_name` · VARCHAR(64) · NOT NULL
- `role` · VARCHAR(32) · NOT NULL
- `org_id` · INTEGER · → organizations.id
- `token_valid_from` · DATETIME
- `created_at` · DATETIME · NOT NULL
- _index_ ix_users_username(username) UNIQUE

## vaccination_records

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `vaccine_code` · VARCHAR(64) · NOT NULL
- `vaccine_name` · VARCHAR(128) · NOT NULL
- `dose_no` · INTEGER · NOT NULL
- `vaccinated_date` · VARCHAR(10) · NOT NULL
- `org_id` · INTEGER · NOT NULL · → organizations.id
- `batch_id` · INTEGER · index · → vaccine_batches.id
- `batch_no` · VARCHAR(64) · NOT NULL · index
- `site` · VARCHAR(32) · NOT NULL
- `vaccinator` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_vaccination_records_batch_id(batch_id)
- _index_ ix_vaccination_records_batch_no(batch_no)
- _index_ ix_vaccination_records_created_at(created_at)
- _index_ ix_vaccination_records_patient_id(patient_id)

## vaccine_batches

- `id` · INTEGER · PK · NOT NULL
- `vaccine_code` · VARCHAR(64) · NOT NULL · index
- `vaccine_name` · VARCHAR(128) · NOT NULL
- `batch_no` · VARCHAR(64) · NOT NULL · index
- `manufacturer` · VARCHAR(128) · NOT NULL
- `expire_date` · VARCHAR(10) · NOT NULL · index
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `quantity` · INTEGER · NOT NULL
- `used_quantity` · INTEGER · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `frozen_reason` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (vaccine_code, batch_no) uq_vaccine_batch
- _index_ ix_vaccine_batches_batch_no(batch_no)
- _index_ ix_vaccine_batches_expire_date(expire_date)
- _index_ ix_vaccine_batches_org_id(org_id)
- _index_ ix_vaccine_batches_status(status)
- _index_ ix_vaccine_batches_vaccine_code(vaccine_code)

## vaccine_contraindications

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `vaccine_code` · VARCHAR(64) · NOT NULL
- `reason` · VARCHAR(256) · NOT NULL
- `contra_type` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `valid_until` · VARCHAR(10) · NOT NULL
- `lifted_by` · INTEGER · → users.id
- `lifted_at` · DATETIME
- `lift_reason` · VARCHAR(256) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_vaccine_contraindications_contra_type(contra_type)
- _index_ ix_vaccine_contraindications_patient_id(patient_id)
- _index_ ix_vaccine_contraindications_status(status)

## visit_credentials

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `credential_no` · VARCHAR(64) · NOT NULL · index
- `org_id` · INTEGER · index · → organizations.id
- `credential_type` · VARCHAR(16) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `issued_by` · INTEGER · → users.id
- `issued_at` · DATETIME · NOT NULL
- `closed_at` · DATETIME
- `close_reason` · VARCHAR(128) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_visit_credentials_created_at(created_at)
- _index_ ix_visit_credentials_credential_no(credential_no) UNIQUE
- _index_ ix_visit_credentials_credential_type(credential_type)
- _index_ ix_visit_credentials_org_id(org_id)
- _index_ ix_visit_credentials_patient_id(patient_id)
- _index_ ix_visit_credentials_status(status)

## vital_sign_records

- `id` · INTEGER · PK · NOT NULL
- `admission_id` · INTEGER · NOT NULL · index · → admissions.id
- `measured_at` · VARCHAR(16) · NOT NULL · index
- `temperature` · FLOAT
- `pulse` · INTEGER
- `respiration` · INTEGER
- `sbp` · INTEGER
- `dbp` · INTEGER
- `intake_ml` · INTEGER
- `output_ml` · INTEGER
- `weight_kg` · FLOAT
- `recorder` · VARCHAR(64) · NOT NULL
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL
- _index_ ix_vital_sign_records_admission_id(admission_id)
- _index_ ix_vital_sign_records_measured_at(measured_at)

## voucher_entries

- `id` · INTEGER · PK · NOT NULL
- `voucher_id` · INTEGER · NOT NULL · index · → vouchers.id
- `subject_code` · VARCHAR(16) · NOT NULL · index
- `summary` · VARCHAR(256) · NOT NULL
- `debit` · NUMERIC(14, 2) · NOT NULL
- `credit` · NUMERIC(14, 2) · NOT NULL
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_voucher_entries_created_at(created_at)
- _index_ ix_voucher_entries_subject_code(subject_code)
- _index_ ix_voucher_entries_voucher_id(voucher_id)

## vouchers

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `period` · VARCHAR(7) · NOT NULL · index
- `voucher_no` · VARCHAR(32) · NOT NULL
- `voucher_date` · VARCHAR(10) · NOT NULL · index
- `summary` · VARCHAR(256) · NOT NULL
- `total_debit` · NUMERIC(14, 2) · NOT NULL
- `total_credit` · NUMERIC(14, 2) · NOT NULL
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `posted_by` · INTEGER · → users.id
- `posted_at` · DATETIME
- `created_at` · DATETIME · NOT NULL · index
- _unique_ (org_id, voucher_no) uq_voucher_org_no
- _index_ ix_vouchers_created_at(created_at)
- _index_ ix_vouchers_org_id(org_id)
- _index_ ix_vouchers_period(period)
- _index_ ix_vouchers_status(status)
- _index_ ix_vouchers_voucher_date(voucher_date)

## wards

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `name` · VARCHAR(64) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _unique_ (org_id, name) uq_ward_org_name
- _index_ ix_wards_org_id(org_id)

## waste_locations

- `id` · INTEGER · PK · NOT NULL
- `org_id` · INTEGER · NOT NULL · index · → organizations.id
- `name` · VARCHAR(128) · NOT NULL
- `location_type` · VARCHAR(16) · NOT NULL · index
- `manager_name` · VARCHAR(64) · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_waste_locations_active(active)
- _index_ ix_waste_locations_location_type(location_type)
- _index_ ix_waste_locations_org_id(org_id)

## women_health_records

- `id` · INTEGER · PK · NOT NULL
- `patient_id` · INTEGER · NOT NULL · index · → patients.id
- `record_type` · VARCHAR(16) · NOT NULL · index
- `org_id` · INTEGER · index · → organizations.id
- `exam_date` · VARCHAR(10) · NOT NULL
- `result` · VARCHAR(512) · NOT NULL
- `advice` · VARCHAR(512) · NOT NULL
- `created_at` · DATETIME · NOT NULL
- _index_ ix_women_health_records_org_id(org_id)
- _index_ ix_women_health_records_patient_id(patient_id)
- _index_ ix_women_health_records_record_type(record_type)

## workflow_definitions

- `id` · INTEGER · PK · NOT NULL
- `key` · VARCHAR(48) · NOT NULL · index
- `name` · VARCHAR(128) · NOT NULL
- `nodes` · JSON · NOT NULL
- `active` · BOOLEAN · NOT NULL · index
- `created_at` · DATETIME · NOT NULL
- _index_ ix_workflow_definitions_active(active)
- _index_ ix_workflow_definitions_key(key) UNIQUE

## workflow_instances

- `id` · INTEGER · PK · NOT NULL
- `definition_key` · VARCHAR(48) · NOT NULL · index
- `business_type` · VARCHAR(32) · NOT NULL · index
- `business_id` · INTEGER · NOT NULL · index
- `title` · VARCHAR(256) · NOT NULL
- `org_id` · INTEGER · index · → organizations.id
- `current_node` · VARCHAR(48) · NOT NULL · index
- `status` · VARCHAR(16) · NOT NULL · index
- `created_by` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- `updated_at` · DATETIME · NOT NULL
- _index_ ix_workflow_instances_business_id(business_id)
- _index_ ix_workflow_instances_business_type(business_type)
- _index_ ix_workflow_instances_created_at(created_at)
- _index_ ix_workflow_instances_current_node(current_node)
- _index_ ix_workflow_instances_definition_key(definition_key)
- _index_ ix_workflow_instances_org_id(org_id)
- _index_ ix_workflow_instances_status(status)

## workflow_transitions

- `id` · INTEGER · PK · NOT NULL
- `instance_id` · INTEGER · NOT NULL · index · → workflow_instances.id
- `from_node` · VARCHAR(48) · NOT NULL
- `to_node` · VARCHAR(48) · NOT NULL
- `action` · VARCHAR(16) · NOT NULL
- `comment` · VARCHAR(512) · NOT NULL
- `actor_id` · INTEGER · NOT NULL · → users.id
- `created_at` · DATETIME · NOT NULL · index
- _index_ ix_workflow_transitions_created_at(created_at)
- _index_ ix_workflow_transitions_instance_id(instance_id)
