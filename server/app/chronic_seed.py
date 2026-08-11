"""慢病病种目录种子数据（指引㉒㉗：慢病一体化管理与医防协同）。

覆盖 8 个县域重点管理病种，启动时（main.lifespan）幂等入库为 ChronicDiseaseType：
高血压、2型糖尿病、慢阻肺、冠心病、脑卒中、严重精神障碍、恶性肿瘤、结核病。

level_rules JSON 结构（分级规则，3=高危需转诊评估 / 2=需干预 / 1=控制良好）::

    {
      "require_all": true,             # true=全部指标齐备才评级（缺一项则维持原级）
      "metrics": [
        {"key": "sbp",                 # 指标名：优先取 FollowUp 同名列，其次取 metrics JSON
         "name": "收缩压", "unit": "mmHg",
         "direction": "high",          # high=数值越高越危；low=数值越低越危
         "level3": 160, "level2": 140} # 阈值区间：≥160→3级，≥140→2级，其余1级
      ]
    }

原三病种（hypertension/diabetes/copd）编码保持不变，血压/血糖阈值与既有硬编码逻辑一致，
迁移后行为不变；新增病种指标统一走 FollowUp.metrics 通用 JSON 字段。
"""

# (code, name, level_rules, guidance, followup_interval_days)
SEED_CHRONIC_DISEASE_TYPES: list[dict] = [
    {
        "code": "hypertension",
        "name": "高血压",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "sbp", "name": "收缩压", "unit": "mmHg", "direction": "high", "level3": 160, "level2": 140},
                {"key": "dbp", "name": "舒张压", "unit": "mmHg", "direction": "high", "level3": 100, "level2": 90},
            ],
        },
        "guidance": "限盐（每日<5g）、控制体重、戒烟限酒、每周≥150分钟中等强度运动、规律服药并自测血压",
        "followup_interval_days": 90,
    },
    {
        "code": "diabetes",
        "name": "2型糖尿病",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "glucose", "name": "空腹血糖", "unit": "mmol/L", "direction": "high", "level3": 10.0, "level2": 7.0},
            ],
        },
        "guidance": "控制总能量摄入、主食粗细搭配、规律三餐、餐后适量运动、监测血糖、遵医嘱用药",
        "followup_interval_days": 90,
    },
    {
        "code": "copd",
        "name": "慢阻肺",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "cat_score", "name": "CAT评分", "unit": "分", "direction": "high", "level3": 20, "level2": 10},
            ],
        },
        "guidance": "戒烟、防寒保暖避免感染、腹式呼吸与缩唇呼吸锻炼、适度有氧运动、规范吸入用药",
        "followup_interval_days": 90,
    },
    {
        "code": "chd",
        "name": "冠心病",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "angina_weekly", "name": "每周心绞痛发作次数", "unit": "次/周", "direction": "high", "level3": 3, "level2": 1},
            ],
        },
        "guidance": "低盐低脂膳食、戒烟限酒、规律有氧运动（避免剧烈）、控制血压血脂血糖、随身备硝酸酯类急救药、按时服用抗血小板与他汀",
        "followup_interval_days": 90,
    },
    {
        "code": "stroke",
        "name": "脑卒中",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "mrs_score", "name": "改良Rankin评分", "unit": "分", "direction": "high", "level3": 4, "level2": 2},
            ],
        },
        "guidance": "控制血压血糖血脂、规范抗栓治疗、低盐低脂膳食、尽早规律康复训练、防跌倒与防误吸、识别卒中预警症状即刻就医",
        "followup_interval_days": 90,
    },
    {
        "code": "severe_mental",
        "name": "严重精神障碍",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "adherence_score", "name": "用药依从性评分", "unit": "分(0-10)", "direction": "low", "level3": 3, "level2": 6},
            ],
        },
        "guidance": "规律服药不擅自停减、家属参与监护与危险行为评估、保证睡眠规律作息、避免饮酒与刺激性物质、定期复诊与免费服药政策告知",
        "followup_interval_days": 90,
    },
    {
        "code": "cancer",
        "name": "恶性肿瘤",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "ecog_score", "name": "ECOG体力状况评分", "unit": "分(0-4)", "direction": "high", "level3": 3, "level2": 1},
            ],
        },
        "guidance": "遵医嘱完成治疗与复查周期、加强营养支持保证蛋白摄入、疼痛规范化管理、预防感染、关注心理疏导与安宁疗护衔接",
        "followup_interval_days": 180,
    },
    {
        "code": "tuberculosis",
        "name": "结核病",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "missed_doses", "name": "月漏服次数", "unit": "次/月", "direction": "high", "level3": 3, "level2": 1},
            ],
        },
        "guidance": "全程规律服药不中断（督导服药）、单独居室通风、咳嗽戴口罩与痰液消毒、加强营养与休息、密切接触者筛查、定期查痰与肝功能",
        "followup_interval_days": 30,
    },
]
