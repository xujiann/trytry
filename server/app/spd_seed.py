"""全域慢专病系统种子数据：病种规则、管理目标、量表、考核指标、积分规则、随访方案。

种子的边界：**预置可直接开工的县域通用口径，不预置各县的个性化配置。**

- 预置：六类重点慢病 + 两类专病的纳入/排除规则、管理阶段、控制目标、
  高危筛查量表、通用考核指标、村医积分规则、出院/术后随访方案与问卷；
- 不预置：具体路径模板的节点编排、考核方案的指标权重组合、服务包与商品——
  这些各县差异极大（`disease_programs` 那轮已经验证过：预置只会被删掉重配）。

全部按唯一编码幂等写入，已存在的不覆盖：实施期现场调过的参数，
不该被一次重启改回出厂值。
"""

# ============================================================ 病种（专病档案）

SEED_PROGRAMS = [
    {
        "code": "hypertension",
        "name": "高血压",
        "category": "chronic",
        "description": "县域重点慢病，以血压达标与并发症防控为管理主线",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["I10", "I11", "I12", "I13", "I15"],
             "label": "确诊高血压"},
        ],
        "exclude_rules": [
            {"field": "age", "op": "<", "value": 18, "label": "未成年人不纳入成人高血压管理"},
        ],
        "stages": [
            {"key": "screen", "name": "筛查", "alert_days": 30},
            {"key": "diagnose", "name": "诊断评估", "alert_days": 15},
            {"key": "treat", "name": "治疗干预", "alert_days": 90},
            {"key": "stable", "name": "稳定期管理", "alert_days": 180},
        ],
        "milestones": [
            {"key": "first_visit", "name": "首诊", "trigger": "encounter"},
            {"key": "risk_assess", "name": "风险评估", "trigger": "assessment"},
        ],
    },
    {
        "code": "diabetes",
        "name": "2型糖尿病",
        "category": "chronic",
        "description": "以血糖达标、并发症筛查与生活方式干预为主线",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["E11"], "label": "确诊2型糖尿病"},
        ],
        "exclude_rules": [
            {"field": "diagnosis", "op": "in", "value": ["E10"], "label": "1型糖尿病另行管理"},
        ],
        "stages": [
            {"key": "screen", "name": "筛查", "alert_days": 30},
            {"key": "diagnose", "name": "诊断评估", "alert_days": 15},
            {"key": "treat", "name": "治疗干预", "alert_days": 90},
            {"key": "stable", "name": "稳定期管理", "alert_days": 180},
        ],
        "milestones": [
            {"key": "complication_screen", "name": "并发症筛查", "trigger": "exam"},
        ],
    },
    {
        "code": "stroke",
        "name": "脑卒中",
        "category": "chronic",
        "description": "院前—院中—院后全程管理，重点在二级预防与康复",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["I63", "I61", "I60", "G45"],
             "label": "脑卒中或短暂性脑缺血发作"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "pre", "name": "院前", "alert_days": 7},
            {"key": "acute", "name": "急性期", "alert_days": 14},
            {"key": "rehab", "name": "康复期", "alert_days": 90},
            {"key": "post", "name": "院后管理", "alert_days": 180},
        ],
        "milestones": [
            {"key": "onset", "name": "发病", "trigger": "emergency"},
            {"key": "discharge", "name": "出院", "trigger": "admission"},
        ],
    },
    {
        "code": "chd",
        "name": "冠心病",
        "category": "chronic",
        "description": "以规范用药、复诊复查与危险因素控制为主线",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["I20", "I21", "I25"], "label": "冠心病"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "diagnose", "name": "诊断评估", "alert_days": 15},
            {"key": "treat", "name": "治疗干预", "alert_days": 90},
            {"key": "stable", "name": "稳定期管理", "alert_days": 180},
        ],
        "milestones": [{"key": "pci", "name": "介入治疗", "trigger": "surgery"}],
    },
    {
        "code": "copd",
        "name": "慢性阻塞性肺疾病",
        "category": "chronic",
        "description": "以肺功能维持、急性加重预防与家庭氧疗指导为主线",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["J44", "J43"], "label": "慢阻肺"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "diagnose", "name": "诊断评估", "alert_days": 15},
            {"key": "treat", "name": "治疗干预", "alert_days": 90},
            {"key": "stable", "name": "稳定期管理", "alert_days": 180},
        ],
        "milestones": [{"key": "exacerbation", "name": "急性加重", "trigger": "encounter"}],
    },
    {
        "code": "ckd",
        "name": "慢性肾脏病",
        "category": "chronic",
        "description": "以肾功能分期、并发症监测与延缓进展为主线",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["N18"], "label": "慢性肾脏病"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "diagnose", "name": "分期评估", "alert_days": 15},
            {"key": "treat", "name": "治疗干预", "alert_days": 90},
            {"key": "stable", "name": "稳定期管理", "alert_days": 180},
        ],
        "milestones": [{"key": "dialysis", "name": "进入透析", "trigger": "encounter"}],
    },
    {
        "code": "af",
        "name": "心房颤动",
        "category": "specialty",
        "description": "专病口径：抗凝管理、卒中风险评估与节律控制",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["I48"], "label": "心房颤动"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "diagnose", "name": "确诊评估", "alert_days": 15},
            {"key": "anticoag", "name": "抗凝管理", "alert_days": 30},
            {"key": "follow", "name": "长期随访", "alert_days": 90},
        ],
        "milestones": [{"key": "ablation", "name": "射频消融", "trigger": "surgery"}],
    },
    {
        "code": "osteoporosis",
        "name": "骨质疏松症",
        "category": "specialty",
        "description": "专病口径：骨密度筛查、抗骨松治疗与跌倒骨折预防",
        "include_rules": [
            {"field": "diagnosis", "op": "in", "value": ["M80", "M81"], "label": "骨质疏松症"},
        ],
        "exclude_rules": [],
        "stages": [
            {"key": "screen", "name": "筛查", "alert_days": 60},
            {"key": "treat", "name": "抗骨松治疗", "alert_days": 90},
            {"key": "follow", "name": "长期随访", "alert_days": 180},
        ],
        "milestones": [{"key": "fracture", "name": "脆性骨折", "trigger": "encounter"}],
    },
]

# ============================================================ 管理目标

#: (病种, 阶段, 指标, 名称, 下限, 上限, 单位, 随访周期天)
SEED_TARGETS = [
    ("hypertension", "treat", "bp_sys", "收缩压", None, 140.0, "mmHg", 30),
    ("hypertension", "treat", "bp_dia", "舒张压", None, 90.0, "mmHg", 30),
    ("hypertension", "stable", "bp_sys", "收缩压", None, 140.0, "mmHg", 90),
    ("hypertension", "stable", "bp_dia", "舒张压", None, 90.0, "mmHg", 90),
    ("diabetes", "treat", "glucose_fasting", "空腹血糖", 4.4, 7.0, "mmol/L", 30),
    ("diabetes", "treat", "hba1c", "糖化血红蛋白", None, 7.0, "%", 90),
    ("diabetes", "stable", "glucose_fasting", "空腹血糖", 4.4, 7.0, "mmol/L", 90),
    ("stroke", "post", "bp_sys", "收缩压", None, 140.0, "mmHg", 30),
    ("stroke", "post", "ldl", "低密度脂蛋白", None, 1.8, "mmol/L", 90),
    ("chd", "stable", "ldl", "低密度脂蛋白", None, 1.8, "mmol/L", 90),
    ("copd", "stable", "spo2", "血氧饱和度", 92.0, None, "%", 90),
    ("ckd", "treat", "egfr", "肾小球滤过率", 30.0, None, "ml/min", 60),
    ("af", "anticoag", "bp_sys", "收缩压", None, 140.0, "mmHg", 30),
    ("osteoporosis", "treat", "bmi", "体质指数", 18.5, 28.0, "kg/m²", 90),
]

#: 定性目标（"病情稳定"这类没有数值区间的目标）
SEED_QUALITATIVE_TARGETS = [
    ("hypertension", "stable", "condition_stable", "病情稳定", "无头晕、无靶器官损害进展"),
    ("copd", "stable", "condition_stable", "病情稳定", "近3个月无急性加重"),
    ("stroke", "rehab", "adl_improve", "日常生活能力改善", "ADL 评分较入组提升"),
    ("af", "follow", "condition_stable", "病情稳定", "无卒中事件、无出血事件"),
]

# ============================================================ 高危筛查量表

SEED_SCALES = [
    {
        "code": "scr_hypertension",
        "name": "高血压高危筛查问卷",
        "category": "screen",
        "program_code": "hypertension",
        "items": [
            {"key": "family", "title": "直系亲属有高血压病史", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "salt", "title": "口味偏咸/每日食盐超过6克", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "overweight", "title": "超重或肥胖（BMI≥24）", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "smoke", "title": "吸烟", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
            {"key": "drink", "title": "长期大量饮酒", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
            {"key": "symptom", "title": "近期反复头晕、头痛", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
        ],
        "scoring": {"ranges": [
            {"min": 0, "max": 2, "risk": "low", "advice": "保持健康生活方式，每年测量血压1次"},
            {"min": 3, "max": 5, "risk": "mid", "advice": "建议每半年测量血压，控制体重与食盐摄入"},
            {"min": 6, "max": None, "risk": "high", "advice": "建议尽快到基层医疗机构复核血压并评估"},
        ]},
    },
    {
        "code": "scr_diabetes",
        "name": "糖尿病高危筛查问卷",
        "category": "screen",
        "program_code": "diabetes",
        "items": [
            {"key": "age", "title": "年龄≥45岁", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "family", "title": "直系亲属有糖尿病", "type": "single",
             "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]},
            {"key": "overweight", "title": "超重或肥胖", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "inactive", "title": "缺乏体力活动", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
            {"key": "gdm", "title": "有妊娠糖尿病史或巨大儿分娩史", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "symptom", "title": "多饮、多尿、多食或不明原因消瘦", "type": "single",
             "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]},
        ],
        "scoring": {"ranges": [
            {"min": 0, "max": 3, "risk": "low", "advice": "保持健康饮食与运动，每3年检测1次血糖"},
            {"min": 4, "max": 6, "risk": "mid", "advice": "建议每年检测空腹血糖"},
            {"min": 7, "max": None, "risk": "high", "advice": "建议尽快检测空腹血糖与糖化血红蛋白"},
        ]},
    },
    {
        "code": "scr_stroke",
        "name": "脑卒中高危筛查问卷",
        "category": "screen",
        "program_code": "stroke",
        "items": [
            {"key": "hbp", "title": "高血压且未规律控制", "type": "single",
             "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]},
            {"key": "af", "title": "房颤或心瓣膜病", "type": "single",
             "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]},
            {"key": "dm", "title": "糖尿病", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "lipid", "title": "血脂异常", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
            {"key": "smoke", "title": "吸烟", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
            {"key": "history", "title": "既往卒中或短暂性脑缺血发作", "type": "single",
             "options": [{"label": "是", "score": 4}, {"label": "否", "score": 0}]},
        ],
        "scoring": {"ranges": [
            {"min": 0, "max": 2, "risk": "low", "advice": "保持健康生活方式，定期体检"},
            {"min": 3, "max": 5, "risk": "mid", "advice": "建议控制危险因素并每半年随访"},
            {"min": 6, "max": None, "risk": "high", "advice": "建议尽快到卒中中心评估，必要时上转"},
        ]},
    },
    {
        "code": "assess_risk_common",
        "name": "慢专病综合风险评估量表",
        "category": "risk",
        "program_code": "",
        "items": [
            {"key": "control", "title": "近3个月指标控制情况", "type": "single",
             "options": [{"label": "达标", "score": 0}, {"label": "部分达标", "score": 2},
                         {"label": "未达标", "score": 4}]},
            {"key": "adherence", "title": "服药依从性", "type": "single",
             "options": [{"label": "良好", "score": 0}, {"label": "一般", "score": 2},
                         {"label": "差", "score": 4}]},
            {"key": "complication", "title": "并发症情况", "type": "single",
             "options": [{"label": "无", "score": 0}, {"label": "1项", "score": 3},
                         {"label": "2项及以上", "score": 6}]},
            {"key": "selfcare", "title": "自我管理能力", "type": "single",
             "options": [{"label": "能自理", "score": 0}, {"label": "需协助", "score": 2},
                         {"label": "完全依赖", "score": 4}]},
        ],
        "scoring": {"ranges": [
            {"min": 0, "max": 3, "risk": "low", "advice": "维持现有管理方案，按期随访"},
            {"min": 4, "max": 8, "risk": "mid", "advice": "加强随访频次与用药指导"},
            {"min": 9, "max": 13, "risk": "high", "advice": "纳入重点管理，缩短随访周期并评估上转"},
            {"min": 14, "max": None, "risk": "very_high", "advice": "建议上转上级医院评估处置"},
        ]},
    },
]

# ============================================================ 考核指标库

#: 指标的 `data_source` 决定取数口径，实现见 `routers/spd_assess.py::INDICATOR_METRICS`。
SEED_INDICATORS = [
    {"code": "enroll_rate", "name": "纳管率", "object_type": "org", "data_source": "enrollment",
     "formula": "enrolled / target * 100", "weight": 15.0, "target_value": 80.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 80}},
    {"code": "followup_rate", "name": "随访完成率", "object_type": "org", "data_source": "task",
     "formula": "done / total * 100", "weight": 20.0, "target_value": 90.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 90}},
    {"code": "path_rate", "name": "路径完成率", "object_type": "org", "data_source": "path",
     "formula": "completed / total * 100", "weight": 15.0, "target_value": 85.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 85}},
    {"code": "referral_closure_rate", "name": "转诊闭环率", "object_type": "org",
     "data_source": "referral", "formula": "closed / total * 100", "weight": 15.0,
     "target_value": 85.0, "score_rule": {"type": "ratio", "full": 100, "target": 85}},
    {"code": "control_rate", "name": "指标控制达标率", "object_type": "org",
     "data_source": "measurement", "formula": "normal / total * 100", "weight": 15.0,
     "target_value": 70.0, "score_rule": {"type": "ratio", "full": 100, "target": 70}},
    {"code": "assess_rate", "name": "风险评估完成率", "object_type": "org",
     "data_source": "assessment", "formula": "assessed / enrolled * 100", "weight": 10.0,
     "target_value": 90.0, "score_rule": {"type": "ratio", "full": 100, "target": 90}},
    {"code": "archive_rate", "name": "建档完整率", "object_type": "org", "data_source": "archive",
     "formula": "archived / enrolled * 100", "weight": 10.0, "target_value": 95.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 95}},
    {"code": "vd_sign", "name": "村医签约数", "object_type": "village_doctor",
     "data_source": "enrollment", "formula": "enrolled", "weight": 25.0, "target_value": 30.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 30}},
    {"code": "vd_referral_up", "name": "村医有效上转数", "object_type": "village_doctor",
     "data_source": "referral", "formula": "effective", "weight": 25.0, "target_value": 5.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 5}},
    {"code": "vd_followup", "name": "村医随访完成率", "object_type": "village_doctor",
     "data_source": "task", "formula": "done / total * 100", "weight": 30.0, "target_value": 90.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 90}},
    {"code": "vd_abnormal_report", "name": "村医异常上报数", "object_type": "village_doctor",
     "data_source": "case_report", "formula": "reported", "weight": 20.0, "target_value": 3.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 3}},
    {"code": "doctor_workload", "name": "医生服务工作量", "object_type": "doctor",
     "data_source": "task", "formula": "done", "weight": 100.0, "target_value": 50.0,
     "score_rule": {"type": "ratio", "full": 100, "target": 50}},
]

# ============================================================ 村医积分规则

SEED_POINT_RULES = [
    {"code": "pt_sign", "name": "签约纳管", "event": "sign", "points": 5, "daily_limit": 50},
    {"code": "pt_ref_up", "name": "有效上转", "event": "referral_up", "points": 10,
     "daily_limit": 50},
    {"code": "pt_ref_down", "name": "下转承接", "event": "referral_down", "points": 8,
     "daily_limit": 40},
    {"code": "pt_followup", "name": "随访完成", "event": "followup", "points": 3,
     "daily_limit": 30},
    {"code": "pt_abnormal", "name": "异常上报", "event": "abnormal_report", "points": 5,
     "daily_limit": 25},
    {"code": "pt_signin", "name": "每日签到", "event": "signin", "points": 1, "daily_limit": 1},
]

# ============================================================ 随访问卷与方案

SEED_QUESTIONNAIRES = [
    {
        "code": "q_discharge",
        "name": "出院随访通用问卷",
        "scene": "inpatient",
        "items": [
            {"key": "recovery", "title": "总体恢复情况", "type": "single",
             "options": [{"label": "良好"}, {"label": "一般"}, {"label": "较差"}]},
            {"key": "pain", "title": "疼痛评分（0-10）", "type": "number"},
            {"key": "medicine", "title": "是否遵医嘱服药", "type": "single",
             "options": [{"label": "是"}, {"label": "否"}]},
            {"key": "fever", "title": "是否发热", "type": "single",
             "options": [{"label": "否"}, {"label": "是"}]},
        ],
        "abnormal_rules": [
            {"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high",
             "action": "通知主管医师，必要时安排复诊"},
            {"when": {"field": "fever", "op": "==", "value": "是"}, "level": "mid",
             "action": "指导监测体温，24小时内回访"},
            {"when": {"field": "medicine", "op": "==", "value": "否"}, "level": "low",
             "action": "开展用药指导"},
        ],
        "handle_role": "doctor",
        "preset": True,
    },
    {
        "code": "q_surgery",
        "name": "术后随访问卷",
        "scene": "surgery",
        "items": [
            {"key": "wound", "title": "切口愈合情况", "type": "single",
             "options": [{"label": "良好"}, {"label": "红肿"}, {"label": "渗液"}]},
            {"key": "pain", "title": "疼痛评分（0-10）", "type": "number"},
            {"key": "function", "title": "功能恢复", "type": "single",
             "options": [{"label": "良好"}, {"label": "一般"}, {"label": "较差"}]},
        ],
        "abnormal_rules": [
            {"when": {"field": "wound", "op": "==", "value": "渗液"}, "level": "high",
             "action": "立即联系手术医师，安排返院处理"},
            {"when": {"field": "pain", "op": ">=", "value": 7}, "level": "mid",
             "action": "评估镇痛方案"},
        ],
        "handle_role": "doctor",
        "preset": True,
    },
    {
        "code": "q_chronic",
        "name": "慢病随访问卷",
        "scene": "outpatient",
        "items": [
            {"key": "symptom", "title": "近期症状", "type": "multi",
             "options": [{"label": "无"}, {"label": "头晕"}, {"label": "胸闷"}, {"label": "气促"}]},
            {"key": "adherence", "title": "服药依从性", "type": "single",
             "options": [{"label": "规律"}, {"label": "偶尔漏服"}, {"label": "经常漏服"}]},
            {"key": "bp_sys", "title": "自测收缩压", "type": "number"},
            {"key": "glucose_fasting", "title": "自测空腹血糖", "type": "number"},
        ],
        "abnormal_rules": [
            {"when": {"field": "bp_sys", "op": ">=", "value": 180}, "level": "high",
             "action": "立即上转评估"},
            {"when": {"field": "bp_sys", "op": ">=", "value": 160}, "level": "mid",
             "action": "两周内复诊调整用药"},
            {"when": {"field": "adherence", "op": "==", "value": "经常漏服"}, "level": "low",
             "action": "开展用药指导与宣教"},
        ],
        "handle_role": "doctor",
        "preset": True,
    },
]

SEED_FOLLOWUP_RULES = [
    {"code": "fr_discharge", "name": "出院常规随访方案", "scene": "inpatient",
     "points": [1, 7, 30], "questionnaire_code": "q_discharge", "executor_role": "nurse",
     "preset": True},
    {"code": "fr_surgery", "name": "术后随访方案", "scene": "surgery",
     "points": [1, 7, 30, 90], "questionnaire_code": "q_surgery", "executor_role": "nurse",
     "preset": True},
    {"code": "fr_chronic", "name": "慢病门诊随访方案", "scene": "outpatient",
     "points": [30, 90, 180], "questionnaire_code": "q_chronic", "executor_role": "doctor",
     "preset": True},
]

# ============================================================ 报告模板

SEED_REPORT_TEMPLATES = [
    {
        "code": "rpt_daily",
        "name": "慢专病运行日报",
        "period": "daily",
        "scope_level": "center",
        "sections": [
            {"key": "summary", "title": "总体概览", "type": "text"},
            {"key": "todo", "title": "今日待办", "type": "table"},
            {"key": "alert", "title": "预警提醒", "type": "table"},
        ],
        "variables": {"period_label": "统计日期", "org_name": "机构名称"},
    },
    {
        "code": "rpt_weekly",
        "name": "慢专病服务周报",
        "period": "weekly",
        "scope_level": "dept",
        "sections": [
            {"key": "summary", "title": "本周概览", "type": "text"},
            {"key": "workload", "title": "服务工作量", "type": "table"},
            {"key": "quality", "title": "服务质量", "type": "chart"},
        ],
        "variables": {"period_label": "统计周次"},
    },
    {
        "code": "rpt_monthly",
        "name": "慢专病考核月报",
        "period": "monthly",
        "scope_level": "grassroots",
        "sections": [
            {"key": "summary", "title": "本月概览", "type": "text"},
            {"key": "score", "title": "考核得分", "type": "table"},
            {"key": "trend", "title": "运行趋势", "type": "chart"},
        ],
        "variables": {"period_label": "统计月份"},
    },
]


def seed_all(db) -> None:
    """幂等写入全部种子数据。在应用启动（lifespan）中调用。"""
    from .models_spd import (
        SpdFollowupRule,
        SpdIndicator,
        SpdPointRule,
        SpdProgram,
        SpdQuestionnaire,
        SpdReportTemplate,
        SpdScale,
        SpdTarget,
    )

    existing_programs = {code for (code,) in db.query(SpdProgram.code).all()}
    for seed in SEED_PROGRAMS:
        if seed["code"] not in existing_programs:
            db.add(SpdProgram(**seed, version="v1", active=True))
    db.commit()

    programs = {p.code: p.id for p in db.query(SpdProgram).all()}
    existing_targets = {
        (t.program_id, t.stage, t.metric) for t in db.query(SpdTarget).all()
    }
    for code, stage, metric, name, low, high, unit, days in SEED_TARGETS:
        pid = programs.get(code)
        if pid is None or (pid, stage, metric) in existing_targets:
            continue
        db.add(
            SpdTarget(
                program_id=pid, stage=stage, metric=metric, metric_name=name,
                kind="quantitative", target_low=low, target_high=high, unit=unit,
                followup_interval_days=days,
            )
        )
    for code, stage, metric, name, text in SEED_QUALITATIVE_TARGETS:
        pid = programs.get(code)
        if pid is None or (pid, stage, metric) in existing_targets:
            continue
        db.add(
            SpdTarget(
                program_id=pid, stage=stage, metric=metric, metric_name=name,
                kind="qualitative", qualitative=text,
            )
        )
    db.commit()

    existing_scales = {code for (code,) in db.query(SpdScale.code).all()}
    for seed in SEED_SCALES:
        if seed["code"] not in existing_scales:
            db.add(SpdScale(**seed, version="v1", status="published"))
    db.commit()

    existing_indicators = {code for (code,) in db.query(SpdIndicator.code).all()}
    for seed in SEED_INDICATORS:
        if seed["code"] not in existing_indicators:
            db.add(SpdIndicator(**seed, version="v1", active=True))
    db.commit()

    existing_rules = {code for (code,) in db.query(SpdPointRule.code).all()}
    for seed in SEED_POINT_RULES:
        if seed["code"] not in existing_rules:
            db.add(SpdPointRule(**seed))
    db.commit()

    existing_q = {code for (code,) in db.query(SpdQuestionnaire.code).all()}
    for seed in SEED_QUESTIONNAIRES:
        if seed["code"] not in existing_q:
            db.add(SpdQuestionnaire(**seed))
    db.commit()

    existing_fr = {code for (code,) in db.query(SpdFollowupRule.code).all()}
    for seed in SEED_FOLLOWUP_RULES:
        if seed["code"] not in existing_fr:
            db.add(SpdFollowupRule(**seed))
    db.commit()

    existing_rpt = {code for (code,) in db.query(SpdReportTemplate.code).all()}
    for seed in SEED_REPORT_TEMPLATES:
        if seed["code"] not in existing_rpt:
            db.add(SpdReportTemplate(**seed))
    db.commit()
