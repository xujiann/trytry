"""块2：电子病历环节质控规则种子（12 条，对齐《病历书写基本规范》要点）。

字段说明：
- check_field  被检字段：病历结构化字段，或引擎派生字段
  （case_summary=病案首页是否已填、critical_disposal=危急值处置记录文本）
- rule         执行器：required / min_length / max_length / keyword_present
- config       参数：min、max、keywords（任一命中即通过）、condition（触发条件，
               不满足则该条规则本次不参与评分）
- deduct_points 命中缺陷的扣分（100 分制，扣完为止不低于 0）

条件（config.condition）取值：
- has_critical_report  该患者存在危急值报告时才校验
- inpatient_discharged 该患者已办理出院时才校验
"""

SEED_RECORD_QC_RULES = [
    {
        "code": "MRQC01",
        "name": "主诉必填",
        "check_field": "chief_complaint",
        "rule": "required",
        "config": {},
        "deduct_points": 10,
    },
    {
        "code": "MRQC02",
        "name": "主诉简明（不超过20字）",
        "check_field": "chief_complaint",
        "rule": "max_length",
        "config": {"max": 20},
        "deduct_points": 3,
    },
    {
        "code": "MRQC03",
        "name": "现病史不少于50字",
        "check_field": "present_illness",
        "rule": "min_length",
        "config": {"min": 50},
        "deduct_points": 10,
    },
    {
        "code": "MRQC04",
        "name": "现病史须描述起病时间与诱因",
        "check_field": "present_illness",
        "rule": "keyword_present",
        "config": {"keywords": ["天", "小时", "月", "年", "诱因"]},
        "deduct_points": 4,
    },
    {
        "code": "MRQC05",
        "name": "既往史必填",
        "check_field": "past_history",
        "rule": "required",
        "config": {},
        "deduct_points": 6,
    },
    {
        "code": "MRQC06",
        "name": "体格检查必填",
        "check_field": "physical_exam",
        "rule": "required",
        "config": {},
        "deduct_points": 8,
    },
    {
        "code": "MRQC07",
        "name": "体格检查须含生命体征",
        "check_field": "physical_exam",
        "rule": "keyword_present",
        "config": {"keywords": ["体温", "血压", "脉搏", "呼吸", "心率"]},
        "deduct_points": 4,
    },
    {
        "code": "MRQC08",
        "name": "诊断依据必填",
        "check_field": "diagnosis_basis",
        "rule": "required",
        "config": {},
        "deduct_points": 10,
    },
    {
        "code": "MRQC09",
        "name": "诊断依据不少于30字",
        "check_field": "diagnosis_basis",
        "rule": "min_length",
        "config": {"min": 30},
        "deduct_points": 5,
    },
    {
        "code": "MRQC10",
        "name": "治疗方案必填",
        "check_field": "treatment_plan",
        "rule": "required",
        "config": {},
        "deduct_points": 10,
    },
    {
        "code": "MRQC11",
        "name": "危急值须有处置记录",
        "check_field": "critical_disposal",
        "rule": "keyword_present",
        "config": {"keywords": ["危急值", "紧急处置", "抢救"], "condition": "has_critical_report"},
        "deduct_points": 10,
    },
    {
        "code": "MRQC12",
        "name": "出院须有病案首页",
        "check_field": "case_summary",
        "rule": "required",
        "config": {"condition": "inpatient_discharged"},
        "deduct_points": 12,
    },
]
