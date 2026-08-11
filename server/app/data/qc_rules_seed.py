"""数据质控规则种子（块3）：15 条县域医共体常用数据质量规则。

config 结构按 rule_type 区分（执行逻辑见 app/routers/dataquality.py）：

- required   {"field": 字段, "filter": {字段: 值}?}          字段为空/None 即违规
- range      {"field": 字段, "min": 下限?, "max": 上限?,      数值越界或缺失即违规
              "exclusive_min": bool?, "exclusive_max": bool?}
- enum       {"field": 字段, "values": [允许取值...]}          取值不在清单即违规
- cross_ref  {"field": 字段, "skip_empty": bool?,             引用字典/目录不存在即违规
              "ref_code_system": 字典系统编码} 或
             {"field", "ref_table": 表名, "ref_field": 字段}
- logic      {"check": 命名校验, ...校验参数}                  命名逻辑校验（见 _LOGIC_CHECKS）

落地时应由医共体质控办依据《医院信息互联互通标准化成熟度测评》与本地填报要求，
经 /api/dataquality/rules 增删调整；停用规则不参与扫描。
"""

SEED_QC_RULES = [
    {
        "code": "QC001",
        "name": "患者身份证号须符合18位格式与校验位",
        "target_table": "patients",
        "rule_type": "logic",
        "config": {"check": "id_card_checksum", "field": "id_card"},
        "severity": "error",
    },
    {
        "code": "QC002",
        "name": "患者姓名不得为空",
        "target_table": "patients",
        "rule_type": "required",
        "config": {"field": "name"},
        "severity": "error",
    },
    {
        "code": "QC003",
        "name": "患者出生日期宜完整填写",
        "target_table": "patients",
        "rule_type": "required",
        "config": {"field": "birth_date"},
        "severity": "warn",
    },
    {
        "code": "QC004",
        "name": "就诊记录须填写诊断编码",
        "target_table": "encounters",
        "rule_type": "required",
        "config": {"field": "diagnosis_code"},
        "severity": "error",
    },
    {
        "code": "QC005",
        "name": "就诊诊断编码须在 ICD-10 诊断字典内",
        "target_table": "encounters",
        "rule_type": "cross_ref",
        "config": {"field": "diagnosis_code", "ref_code_system": "diagnosis", "skip_empty": True},
        "severity": "error",
    },
    {
        "code": "QC006",
        "name": "检查检验报告须有诊断结论",
        "target_table": "exam_reports",
        "rule_type": "required",
        "config": {"field": "conclusion"},
        "severity": "error",
    },
    {
        "code": "QC007",
        "name": "危急值报告须闭环至处置反馈",
        "target_table": "exam_reports",
        "rule_type": "logic",
        "config": {"check": "critical_closed_loop"},
        "severity": "warn",
    },
    {
        "code": "QC008",
        "name": "处方药品日剂量须大于 0",
        "target_table": "prescription_items",
        "rule_type": "range",
        "config": {"field": "daily_dose", "min": 0, "exclusive_min": True},
        "severity": "error",
    },
    {
        "code": "QC009",
        "name": "处方用药天数须在 1—90 天之间",
        "target_table": "prescription_items",
        "rule_type": "range",
        "config": {"field": "days", "min": 1, "max": 90},
        "severity": "warn",
    },
    {
        "code": "QC010",
        "name": "处方须填写临床诊断",
        "target_table": "prescriptions",
        "rule_type": "required",
        "config": {"field": "diagnosis_name"},
        "severity": "warn",
    },
    {
        "code": "QC011",
        "name": "慢病档案须有下次随访到期日",
        "target_table": "chronic_patients",
        "rule_type": "required",
        "config": {"field": "next_due"},
        "severity": "warn",
    },
    {
        "code": "QC012",
        "name": "慢病病种须在病种目录内",
        "target_table": "chronic_patients",
        "rule_type": "cross_ref",
        "config": {
            "field": "disease",
            "ref_table": "chronic_disease_types",
            "ref_field": "code",
            "skip_empty": True,
        },
        "severity": "error",
    },
    {
        "code": "QC013",
        "name": "慢病随访须记录对应病种指标",
        "target_table": "followups",
        "rule_type": "logic",
        "config": {
            "check": "chronic_followup_indicator",
            "disease_indicators": {
                "hypertension": ["sbp", "dbp"],
                "diabetes": ["glucose"],
                "chd": ["sbp", "dbp"],
                "stroke": ["sbp", "dbp"],
            },
        },
        "severity": "warn",
    },
    {
        "code": "QC014",
        "name": "住院出院时间不得早于入院时间",
        "target_table": "admissions",
        "rule_type": "logic",
        "config": {
            "check": "datetime_order",
            "start_field": "admitted_at",
            "end_field": "discharged_at",
        },
        "severity": "error",
    },
    {
        "code": "QC015",
        "name": "传染病报告发病日期不得晚于当日",
        "target_table": "infectious_cases",
        "rule_type": "logic",
        "config": {"check": "date_not_future", "field": "onset_date"},
        "severity": "error",
    },
]
