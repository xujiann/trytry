"""审方规则库种子：50 条县域常用药品的合理用药规则（块2）。

字段说明（对应 models.DrugRule）：
- drug_code                 药品编码，与 dict_seed.SEED_COMMON_DRUGS 的 WHO ATC 编码一一对应
- max_daily_dose/dose_unit  日剂量上限，超限即转药师审
- interactions              相互作用冲突药品编码（逗号分隔），同方共现即转药师审
- contraindicated_diagnoses 禁忌诊断关键词（逗号分隔），命中处方诊断即转药师审
- special_groups            特殊人群 pregnant/child/elderly，患者命中即转药师审
- renal_hepatic_note        肝肾功能提示（不拦截，供医师/药师参考与剂量调整）
- note                      规则说明
- review_points             处方点评要点（事后点评的规则化依据）

剂量为成人常规最大日剂量参考值，落地时应由医共体药事管理委员会依据说明书与
《处方管理办法》《医疗机构处方审核规范》核定后经 /api/prescriptions/rules 调整。
"""

# 常用相互作用编码速记（便于交叉引用）
_WARFARIN = "B01AA03"
_ASPIRIN = "B01AC06"
_CLOPIDOGREL = "B01AC04"
_RIVAROXABAN = "B01AF01"
_IBUPROFEN = "M01AE01"
_CELECOXIB = "M01AH01"
_OMEPRAZOLE = "A02BC01"
_FLUCONAZOLE = "J02AC01"
_AZITHROMYCIN = "J01FA10"
_LEVOFLOXACIN = "J01MA12"
_SIMVASTATIN = "C10AA01"
_ATORVASTATIN = "C10AA05"
_SPIRONOLACTONE = "C03DA01"
_FUROSEMIDE = "C03CA01"
_CAPTOPRIL = "C09AA01"
_PERINDOPRIL = "C09AA04"
_LOSARTAN = "C09CA01"
_VALSARTAN = "C09CA03"
_METFORMIN = "A10BA02"
_GLICLAZIDE = "A10BB09"
_GLIMEPIRIDE = "A10BB12"
_PREDNISONE = "H02AB07"
_METOPROLOL = "C07AB02"
_SALBUTAMOL = "R03AC02"
_FLUOXETINE = "N06AB03"
_SERTRALINE = "N06AB06"
_ALLOPURINOL = "M04AA01"
_PARACETAMOL = "N02BE01"

# 常见禁忌诊断关键词组
_NSAID_CONTRA = "消化性溃疡,胃溃疡,十二指肠溃疡,消化道出血,活动性出血,严重肾功能不全"
_ANTIPLATELET_CONTRA = "活动性出血,消化道出血,脑出血,血小板减少"

SEED_DRUG_RULES: list[dict] = [
    # ---------- 消化系统 ----------
    {
        "drug_code": _OMEPRAZOLE,
        "max_daily_dose": 40,
        "dose_unit": "mg",
        "note": "质子泵抑制剂，疗程超过8周需评估继续用药指征",
        "interactions": f"{_CLOPIDOGREL}",
        "renal_hepatic_note": "严重肝功能不全日剂量不超过20mg；肾功能不全无需常规调整",
        "review_points": "适应证是否明确；是否长期无指征使用（PPI 滥用）；与氯吡格雷合用降低抗血小板疗效",
    },
    {
        "drug_code": "A02BC02",
        "max_daily_dose": 80,
        "dose_unit": "mg",
        "note": "质子泵抑制剂，静脉给药仅限不能口服者",
        "interactions": "",
        "renal_hepatic_note": "重度肝损害日剂量不超过20mg",
        "review_points": "给药途径是否合理（能口服不静脉）；疗程是否过长",
    },
    {
        "drug_code": "A03FA01",
        "max_daily_dose": 30,
        "dose_unit": "mg",
        "note": "促胃肠动力药，连续使用不宜超过5天（锥体外系反应风险）",
        "contraindicated_diagnoses": "消化道出血,机械性肠梗阻,嗜铬细胞瘤,癫痫",
        "special_groups": "child,elderly",
        "renal_hepatic_note": "肌酐清除率<40ml/min 剂量减半",
        "review_points": "疗程是否超过5天；儿童与老年人锥体外系反应风险是否评估",
    },
    {
        "drug_code": "A06AD11",
        "max_daily_dose": 60,
        "dose_unit": "g",
        "note": "渗透性泻药，慢性便秘一线用药",
        "contraindicated_diagnoses": "肠梗阻,半乳糖血症",
        "renal_hepatic_note": "肝性脑病患者需按血氨与排便次数调整剂量",
        "review_points": "是否排除肠梗阻；肝性脑病用法用量是否规范",
    },
    # ---------- 内分泌/降糖 ----------
    {
        "drug_code": _METFORMIN,
        "max_daily_dose": 2550,
        "dose_unit": "mg",
        "note": "2型糖尿病一线用药，随餐服用减少胃肠反应",
        "contraindicated_diagnoses": "急性肾损伤,严重肾功能不全,乳酸酸中毒,重度心力衰竭,严重感染",
        "renal_hepatic_note": "eGFR 45-59 减量、<45 慎用、<30 禁用；肝功能不全增加乳酸酸中毒风险；含碘造影检查前后停药48小时",
        "review_points": "是否核对肾功能（eGFR）；造影前后是否停药；是否与其他降糖药重复叠加",
    },
    {
        "drug_code": _GLICLAZIDE,
        "max_daily_dose": 320,
        "dose_unit": "mg",
        "note": "磺脲类促泌剂，低血糖风险需交代识别与处置",
        "interactions": f"{_GLIMEPIRIDE},{_FLUCONAZOLE},{_LEVOFLOXACIN}",
        "special_groups": "elderly",
        "renal_hepatic_note": "中重度肾功能不全与肝功能不全者低血糖风险升高，需减量或换药",
        "review_points": "是否与其他磺脲类重复；老年人低血糖风险是否评估；用药时间是否餐前",
    },
    {
        "drug_code": _GLIMEPIRIDE,
        "max_daily_dose": 6,
        "dose_unit": "mg",
        "note": "长效磺脲类，每日一次早餐前顿服",
        "interactions": f"{_GLICLAZIDE},{_FLUCONAZOLE},{_LEVOFLOXACIN}",
        "special_groups": "elderly",
        "renal_hepatic_note": "肾功能不全者起始 1mg 并密切监测血糖",
        "review_points": "给药频次是否为每日一次；是否与其他促泌剂联用",
    },
    {
        "drug_code": "A10BH01",
        "max_daily_dose": 100,
        "dose_unit": "mg",
        "note": "DPP-4 抑制剂，低血糖风险低",
        "renal_hepatic_note": "eGFR 30-45 减为50mg/日，<30 减为25mg/日",
        "review_points": "肾功能分层剂量是否正确；是否与 GLP-1 类重复机制联用",
    },
    {
        "drug_code": "A10BK01",
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "SGLT-2 抑制剂，注意泌尿生殖道感染与容量不足",
        "interactions": _FUROSEMIDE,
        "contraindicated_diagnoses": "1型糖尿病,酮症酸中毒",
        "renal_hepatic_note": "eGFR<25 不建议起始用于降糖；与利尿剂合用需监测血容量",
        "review_points": "适应证是否为2型糖尿病；是否评估容量状态与泌尿系感染史",
    },
    {
        "drug_code": "A10AB05",
        "max_daily_dose": 100,
        "dose_unit": "U",
        "note": "速效胰岛素类似物，餐前即刻皮下注射",
        "special_groups": "elderly",
        "renal_hepatic_note": "肝肾功能不全者胰岛素需求下降，需下调剂量防低血糖",
        "review_points": "注射时间与餐时是否匹配；剂量单位书写是否规范（禁用 U 简写歧义）",
    },
    {
        "drug_code": "A10AE04",
        "max_daily_dose": 80,
        "dose_unit": "U",
        "note": "长效基础胰岛素，每日固定时间注射",
        "special_groups": "elderly",
        "renal_hepatic_note": "肝肾功能不全者需减量并加强血糖监测",
        "review_points": "基础-餐时方案是否合理；是否与预混胰岛素重复",
    },
    # ---------- 抗栓/抗凝 ----------
    {
        "drug_code": _WARFARIN,
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "华法林治疗窗窄，须按 INR 调整（一般目标 2.0-3.0）",
        "interactions": f"{_ASPIRIN},{_CLOPIDOGREL},{_IBUPROFEN},{_CELECOXIB},{_AZITHROMYCIN},{_FLUCONAZOLE},{_LEVOFLOXACIN},{_RIVAROXABAN}",
        "contraindicated_diagnoses": "活动性出血,消化道出血,脑出血,严重肝病",
        "special_groups": "pregnant,elderly",
        "renal_hepatic_note": "肝功能不全者代谢下降、出血风险显著升高；肾功能不全需加密 INR 监测",
        "review_points": "是否监测 INR 并记录目标范围；是否与抗血小板/NSAIDs/抗菌药合用未评估出血风险；妊娠期禁用",
    },
    {
        "drug_code": _CLOPIDOGREL,
        "max_daily_dose": 75,
        "dose_unit": "mg",
        "note": "P2Y12 抑制剂，负荷剂量需单独医嘱注明",
        "interactions": f"{_WARFARIN},{_OMEPRAZOLE},{_ASPIRIN}",
        "contraindicated_diagnoses": _ANTIPLATELET_CONTRA,
        "renal_hepatic_note": "重度肝功能不全者出血风险增加，慎用",
        "review_points": "双抗疗程是否有明确指征与期限；与奥美拉唑合用是否可换用泮托拉唑",
    },
    {
        "drug_code": _ASPIRIN,
        "max_daily_dose": 100,
        "dose_unit": "mg",
        "note": "心脑血管二级预防常规 75-100mg 每日一次",
        "interactions": f"{_WARFARIN},{_CLOPIDOGREL},{_IBUPROFEN},{_RIVAROXABAN}",
        "contraindicated_diagnoses": _ANTIPLATELET_CONTRA + ",哮喘",
        "special_groups": "child,pregnant",
        "renal_hepatic_note": "严重肝肾功能不全禁用；肾功能不全者与 ACEI/利尿剂三联需警惕肾损伤",
        "review_points": "剂量是否为抗血小板剂量；儿童禁用（瑞氏综合征）；与布洛芬合用是否影响抗血小板作用",
    },
    {
        "drug_code": _RIVAROXABAN,
        "max_daily_dose": 20,
        "dose_unit": "mg",
        "note": "口服 Xa 因子抑制剂，房颤抗凝需按肾功能分层",
        "interactions": f"{_WARFARIN},{_ASPIRIN},{_CLOPIDOGREL},{_FLUCONAZOLE}",
        "contraindicated_diagnoses": "活动性出血,脑出血,严重肝病",
        "special_groups": "pregnant",
        "renal_hepatic_note": "肌酐清除率 15-50ml/min 房颤适应证减为15mg/日；<15ml/min 禁用；中重度肝损害伴凝血异常禁用",
        "review_points": "是否按肌酐清除率分层给药；是否与其他抗凝/抗血小板重复；服药是否随餐",
    },
    # ---------- 循环系统 ----------
    {
        "drug_code": "C01DA14",
        "max_daily_dose": 100,
        "dose_unit": "mg",
        "note": "长效硝酸酯，需保留每日无药间期以防耐药",
        "contraindicated_diagnoses": "低血压,肥厚型梗阻性心肌病",
        "renal_hepatic_note": "严重肝肾功能不全者慎用",
        "review_points": "给药间隔是否留有无硝酸酯期；是否与 PDE5 抑制剂合用",
    },
    {
        "drug_code": _FUROSEMIDE,
        "max_daily_dose": 80,
        "dose_unit": "mg",
        "note": "袢利尿剂，需监测电解质与容量",
        "interactions": f"{_SPIRONOLACTONE},{_CAPTOPRIL},{_IBUPROFEN}",
        "contraindicated_diagnoses": "低钾血症,无尿",
        "special_groups": "elderly",
        "renal_hepatic_note": "肾功能不全者需加大剂量方有效但需防耳毒性；肝硬化腹水需联合保钾利尿剂",
        "review_points": "是否监测血钾与肾功能；老年人体位性低血压与跌倒风险",
    },
    {
        "drug_code": _SPIRONOLACTONE,
        "max_daily_dose": 100,
        "dose_unit": "mg",
        "note": "保钾利尿剂，心衰改善预后剂量一般 20-40mg/日",
        "interactions": f"{_CAPTOPRIL},{_PERINDOPRIL},{_LOSARTAN},{_VALSARTAN}",
        "contraindicated_diagnoses": "高钾血症,急性肾损伤,严重肾功能不全",
        "renal_hepatic_note": "eGFR<30 禁用；与 ACEI/ARB 合用高钾风险显著升高，须每 1-2 周复查血钾",
        "review_points": "是否与 ACEI/ARB 合用未监测血钾；剂量是否超过心衰推荐范围",
    },
    {
        "drug_code": _METOPROLOL,
        "max_daily_dose": 200,
        "dose_unit": "mg",
        "note": "β受体阻滞剂，不可骤停（反跳性心动过速）",
        "interactions": f"{_SALBUTAMOL}",
        "contraindicated_diagnoses": "重度心动过缓,二度以上房室传导阻滞,失代偿性心力衰竭,支气管哮喘",
        "renal_hepatic_note": "肝功能不全者血药浓度升高需减量；肾功能不全无需常规调整",
        "review_points": "是否核对心率与传导阻滞；哮喘/慢阻肺患者是否评估；是否交代不可自行停药",
    },
    {
        "drug_code": "C07AB07",
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "高选择性β1阻滞剂，心衰需从小剂量滴定",
        "contraindicated_diagnoses": "重度心动过缓,二度以上房室传导阻滞,失代偿性心力衰竭",
        "renal_hepatic_note": "严重肝肾功能不全日剂量不超过10mg",
        "review_points": "心衰滴定过程是否记录；是否与其他β阻滞剂重复",
    },
    {
        "drug_code": "C08CA01",
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "长效二氢吡啶类 CCB，注意踝部水肿",
        "interactions": f"{_SIMVASTATIN}",
        "special_groups": "elderly",
        "renal_hepatic_note": "肝功能不全者起始 2.5mg 并缓慢加量；肾功能不全无需调整",
        "review_points": "是否与其他 CCB 重复；与辛伐他汀合用需限制他汀剂量；水肿是否误当心衰处理",
    },
    {
        "drug_code": "C08CA02",
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "缓释剂型须整片吞服",
        "renal_hepatic_note": "肝功能不全者需减量",
        "review_points": "缓释片是否被拆分服用；给药频次是否与剂型匹配",
    },
    {
        "drug_code": "C08CA05",
        "max_daily_dose": 60,
        "dose_unit": "mg",
        "note": "普通片起效快，不推荐用于急性降压（舌下含服禁用）",
        "contraindicated_diagnoses": "急性心肌梗死,不稳定型心绞痛",
        "renal_hepatic_note": "肝功能不全者清除减慢需减量",
        "review_points": "是否存在舌下含服降压的不规范用法；剂型与频次是否匹配",
    },
    {
        "drug_code": _CAPTOPRIL,
        "max_daily_dose": 150,
        "dose_unit": "mg",
        "note": "ACEI，餐前1小时服用，注意干咳与首剂低血压",
        "interactions": f"{_SPIRONOLACTONE},{_LOSARTAN},{_VALSARTAN},{_IBUPROFEN}",
        "contraindicated_diagnoses": "双侧肾动脉狭窄,高钾血症,血管神经性水肿",
        "special_groups": "pregnant",
        "renal_hepatic_note": "eGFR<30 需减量；用药后1-2周复查血肌酐与血钾，肌酐升高>30% 应停药",
        "review_points": "妊娠期禁用；是否与 ARB 重复阻断 RAAS；是否复查肾功能与血钾",
    },
    {
        "drug_code": _PERINDOPRIL,
        "max_daily_dose": 8,
        "dose_unit": "mg",
        "note": "长效 ACEI，每日一次晨服",
        "interactions": f"{_SPIRONOLACTONE},{_LOSARTAN},{_VALSARTAN}",
        "contraindicated_diagnoses": "双侧肾动脉狭窄,高钾血症,血管神经性水肿",
        "special_groups": "pregnant",
        "renal_hepatic_note": "eGFR 30-60 起始 2mg；eGFR<30 慎用",
        "review_points": "妊娠期禁用；ACEI 与 ARB 是否联用；干咳是否记录并处理",
    },
    {
        "drug_code": _LOSARTAN,
        "max_daily_dose": 100,
        "dose_unit": "mg",
        "note": "ARB，糖尿病肾病可优先选用",
        "interactions": f"{_CAPTOPRIL},{_PERINDOPRIL},{_SPIRONOLACTONE}",
        "contraindicated_diagnoses": "双侧肾动脉狭窄,高钾血症",
        "special_groups": "pregnant",
        "renal_hepatic_note": "肝功能不全者起始 25mg；重度肾功能不全需监测血钾",
        "review_points": "妊娠期禁用；是否与 ACEI 联用；是否监测血钾与肌酐",
    },
    {
        "drug_code": _VALSARTAN,
        "max_daily_dose": 320,
        "dose_unit": "mg",
        "note": "ARB，高血压常用 80-160mg/日",
        "interactions": f"{_CAPTOPRIL},{_PERINDOPRIL},{_SPIRONOLACTONE}",
        "contraindicated_diagnoses": "双侧肾动脉狭窄,高钾血症",
        "special_groups": "pregnant",
        "renal_hepatic_note": "胆汁性肝硬化或胆道梗阻者禁用；重度肝功能不全慎用",
        "review_points": "妊娠期禁用；是否与 ACEI 重复；复方制剂是否造成成分叠加",
    },
    {
        "drug_code": _SIMVASTATIN,
        "max_daily_dose": 40,
        "dose_unit": "mg",
        "note": "他汀睡前服用，注意肌痛与横纹肌溶解",
        "interactions": f"{_FLUCONAZOLE},{_AZITHROMYCIN},C08CA01",
        "contraindicated_diagnoses": "活动性肝病,不明原因转氨酶持续升高",
        "special_groups": "pregnant",
        "renal_hepatic_note": "活动性肝病禁用；eGFR<30 日剂量不超过20mg；用药前及4-8周复查肝功能与肌酸激酶",
        "review_points": "与氨氯地平合用是否超过20mg 限制；是否复查肝功能与 CK；妊娠期禁用",
    },
    {
        "drug_code": _ATORVASTATIN,
        "max_daily_dose": 80,
        "dose_unit": "mg",
        "note": "中高强度他汀，ASCVD 二级预防基石",
        "interactions": f"{_FLUCONAZOLE},{_AZITHROMYCIN}",
        "contraindicated_diagnoses": "活动性肝病",
        "special_groups": "pregnant",
        "renal_hepatic_note": "肾功能不全无需调整剂量；活动性肝病禁用，肝酶升高>3倍正常上限需停药",
        "review_points": "他汀强度与 LDL-C 目标是否匹配；是否与其他他汀重复；妊娠期禁用",
    },
    {
        "drug_code": "C10AA07",
        "max_daily_dose": 20,
        "dose_unit": "mg",
        "note": "亚洲人群起始剂量建议 5-10mg",
        "contraindicated_diagnoses": "活动性肝病",
        "special_groups": "pregnant",
        "renal_hepatic_note": "重度肾功能不全（eGFR<30）禁用；亚洲人群血药浓度较高需低剂量起始",
        "review_points": "起始剂量是否过高；是否监测肌痛与蛋白尿",
    },
    # ---------- 激素与内分泌 ----------
    {
        "drug_code": _PREDNISONE,
        "max_daily_dose": 60,
        "dose_unit": "mg",
        "note": "糖皮质激素，长期使用需补钙与维生素D，不可骤停",
        "interactions": f"{_IBUPROFEN},{_ASPIRIN}",
        "contraindicated_diagnoses": "活动性消化性溃疡,严重感染,活动性结核",
        "special_groups": "child,elderly,pregnant",
        "renal_hepatic_note": "肝功能不全者宜选用泼尼松龙；长期使用需监测血糖血压与骨密度",
        "review_points": "疗程与减量方案是否明确；是否合用 NSAIDs 增加溃疡风险；儿童生长发育影响是否告知",
    },
    {
        "drug_code": "H03AA01",
        "max_daily_dose": 200,
        "dose_unit": "μg",
        "note": "空腹服用，与钙剂铁剂间隔4小时",
        "contraindicated_diagnoses": "未纠正的肾上腺皮质功能减退,急性心肌梗死",
        "special_groups": "elderly",
        "renal_hepatic_note": "肝肾功能不全无需常规调整，但老年及冠心病者需小剂量起始缓慢加量",
        "review_points": "服药时间是否空腹；起始剂量对老年冠心病是否过大；是否定期复查 TSH",
    },
    # ---------- 抗感染 ----------
    {
        "drug_code": "J01CA04",
        "max_daily_dose": 4000,
        "dose_unit": "mg",
        "note": "青霉素类，用药前须询问过敏史",
        "contraindicated_diagnoses": "青霉素过敏",
        "renal_hepatic_note": "eGFR<30 需延长给药间隔；严重肾功能不全减量",
        "review_points": "是否记录过敏史与皮试结果；是否用于无指征的病毒性上感",
    },
    {
        "drug_code": "J01CR02",
        "max_daily_dose": 3600,
        "dose_unit": "mg",
        "note": "含酶抑制剂复方制剂，克拉维酸每日不超过600mg",
        "contraindicated_diagnoses": "青霉素过敏,既往阿莫西林克拉维酸相关黄疸",
        "renal_hepatic_note": "肝损害病史者禁用；eGFR<30 不推荐使用12:1 剂型",
        "review_points": "是否越级使用含酶抑制剂复方；疗程是否合理；过敏史是否记录",
    },
    {
        "drug_code": "J01DC02",
        "max_daily_dose": 1000,
        "dose_unit": "mg",
        "note": "二代头孢，餐后服用提高吸收",
        "contraindicated_diagnoses": "头孢菌素过敏",
        "renal_hepatic_note": "肌酐清除率<30ml/min 给药间隔延长至12-24小时",
        "review_points": "抗菌药分级管理是否符合处方权限；是否有明确细菌感染证据",
    },
    {
        "drug_code": "J01DD04",
        "max_daily_dose": 4000,
        "dose_unit": "mg",
        "note": "三代头孢，不可与含钙输液同管输注",
        "interactions": _WARFARIN,
        "contraindicated_diagnoses": "头孢菌素过敏",
        "special_groups": "child",
        "renal_hepatic_note": "肝肾功能同时严重受损者日剂量不超过2g；新生儿高胆红素血症禁用",
        "review_points": "是否与含钙制剂同时输注；用药前后是否禁酒（双硫仑样反应）；分级管理是否合规",
    },
    {
        "drug_code": _AZITHROMYCIN,
        "max_daily_dose": 500,
        "dose_unit": "mg",
        "note": "大环内酯类，疗程一般3-5天",
        "interactions": f"{_WARFARIN},{_SIMVASTATIN},{_ATORVASTATIN}",
        "contraindicated_diagnoses": "QT间期延长",
        "renal_hepatic_note": "严重肝功能不全禁用；eGFR<10 慎用",
        "review_points": "疗程是否超长；QT 延长与心律失常风险是否评估；与他汀合用肌病风险",
    },
    {
        "drug_code": _LEVOFLOXACIN,
        "max_daily_dose": 750,
        "dose_unit": "mg",
        "note": "喹诺酮类，18岁以下禁用（影响软骨发育）",
        "interactions": f"{_WARFARIN},{_GLICLAZIDE},{_GLIMEPIRIDE}",
        "contraindicated_diagnoses": "肌腱病变,重症肌无力,癫痫,QT间期延长",
        "special_groups": "child,pregnant",
        "renal_hepatic_note": "肌酐清除率 20-49ml/min 剂量减半；<20ml/min 需大幅延长给药间隔",
        "review_points": "儿童与妊娠期禁用；是否用于单纯性上感等无指征情形；肌腱炎风险是否告知",
    },
    {
        "drug_code": _FLUCONAZOLE,
        "max_daily_dose": 400,
        "dose_unit": "mg",
        "note": "三唑类抗真菌药，强效 CYP 抑制剂",
        "interactions": f"{_WARFARIN},{_SIMVASTATIN},{_ATORVASTATIN},{_GLICLAZIDE},{_GLIMEPIRIDE},{_RIVAROXABAN}",
        "contraindicated_diagnoses": "QT间期延长",
        "special_groups": "pregnant",
        "renal_hepatic_note": "肌酐清除率<50ml/min 维持剂量减半；肝功能异常者需监测肝酶",
        "review_points": "是否有真菌感染证据；相互作用（华法林/他汀/磺脲类）是否评估；妊娠期大剂量禁用",
    },
    # ---------- 镇痛抗炎 ----------
    {
        "drug_code": _IBUPROFEN,
        "max_daily_dose": 2400,
        "dose_unit": "mg",
        "note": "NSAIDs，解热镇痛疗程一般不超过3-5天",
        "interactions": f"{_WARFARIN},{_ASPIRIN},{_CAPTOPRIL},{_FUROSEMIDE},{_PREDNISONE}",
        "contraindicated_diagnoses": _NSAID_CONTRA,
        "special_groups": "pregnant,elderly",
        "renal_hepatic_note": "eGFR<30 禁用；与 ACEI+利尿剂形成「三联打击」显著增加急性肾损伤风险",
        "review_points": "消化性溃疡史是否为禁忌；是否合用 PPI 保护；老年人肾功能与出血风险是否评估",
    },
    {
        "drug_code": _CELECOXIB,
        "max_daily_dose": 400,
        "dose_unit": "mg",
        "note": "选择性 COX-2 抑制剂，心血管高危者慎用",
        "interactions": f"{_WARFARIN},{_ASPIRIN}",
        "contraindicated_diagnoses": _NSAID_CONTRA + ",冠心病,磺胺过敏,脑卒中",
        "special_groups": "pregnant,elderly",
        "renal_hepatic_note": "重度肝功能不全禁用；中度肝损害剂量减半；eGFR<30 不推荐",
        "review_points": "心血管风险是否评估；磺胺过敏史是否询问；是否与其他 NSAIDs 重复",
    },
    {
        "drug_code": _ALLOPURINOL,
        "max_daily_dose": 600,
        "dose_unit": "mg",
        "note": "降尿酸药，急性痛风发作期不宜起始",
        "interactions": "J01CA04",
        "contraindicated_diagnoses": "急性痛风发作",
        "renal_hepatic_note": "eGFR 30-60 起始 50-100mg/日；<30 需大幅减量，重症药疹（HLA-B*5801）风险升高",
        "review_points": "是否在急性发作期起始；肾功能分层起始剂量是否正确；是否告知皮疹立即停药",
    },
    {
        "drug_code": _PARACETAMOL,
        "max_daily_dose": 2000,
        "dose_unit": "mg",
        "note": "解热镇痛首选，须警惕复方感冒药重复叠加",
        "interactions": _WARFARIN,
        "contraindicated_diagnoses": "严重肝功能不全,活动性肝病",
        "renal_hepatic_note": "肝功能不全或长期饮酒者日剂量不超过2g；eGFR<30 需延长给药间隔",
        "review_points": "是否与复方感冒药中的对乙酰氨基酚重复；日累计剂量是否超限；疗程是否超过3天",
    },
    # ---------- 精神神经 ----------
    {
        "drug_code": _FLUOXETINE,
        "max_daily_dose": 60,
        "dose_unit": "mg",
        "note": "SSRI，起效需2-4周，不可骤停",
        "interactions": f"{_SERTRALINE},{_WARFARIN}",
        "contraindicated_diagnoses": "双相情感障碍躁狂发作",
        "special_groups": "child,elderly",
        "renal_hepatic_note": "肝功能不全者半衰期显著延长，需减量或隔日给药",
        "review_points": "是否与其他 SSRI/SNRI 联用（5-羟色胺综合征）；青少年自杀风险是否评估与随访",
    },
    {
        "drug_code": _SERTRALINE,
        "max_daily_dose": 200,
        "dose_unit": "mg",
        "note": "SSRI，餐后服用减轻胃肠反应",
        "interactions": f"{_FLUOXETINE},{_WARFARIN}",
        "contraindicated_diagnoses": "双相情感障碍躁狂发作",
        "special_groups": "child,elderly",
        "renal_hepatic_note": "轻中度肝功能不全需减量或延长给药间隔；肾功能不全无需调整",
        "review_points": "剂量滴定是否规范；是否重复使用抗抑郁药；随访周期是否符合严重精神障碍管理要求",
    },
    # ---------- 呼吸系统 ----------
    {
        "drug_code": _SALBUTAMOL,
        "max_daily_dose": 800,
        "dose_unit": "μg",
        "note": "短效β2激动剂，按需使用，用量增多提示控制不佳",
        "interactions": _METOPROLOL,
        "contraindicated_diagnoses": "快速型心律失常",
        "renal_hepatic_note": "肝肾功能不全无需常规调整；低钾血症者需监测血钾",
        "review_points": "按需用量是否提示哮喘控制不佳；是否缺少吸入激素等控制药物；与β阻滞剂拮抗",
    },
    {
        "drug_code": "R03AK06",
        "max_daily_dose": 1000,
        "dose_unit": "μg",
        "note": "ICS/LABA 复方吸入剂，用后需漱口",
        "contraindicated_diagnoses": "活动性肺结核",
        "renal_hepatic_note": "重度肝功能不全者全身暴露增加，需监测皮质功能抑制",
        "review_points": "吸入装置使用方法是否培训；是否漱口预防口腔念珠菌；是否与其他 ICS 重复",
    },
    {
        "drug_code": "R03BB04",
        "max_daily_dose": 18,
        "dose_unit": "μg",
        "note": "长效抗胆碱能药，慢阻肺维持治疗，胶囊仅供吸入禁止口服",
        "contraindicated_diagnoses": "闭角型青光眼,前列腺增生伴排尿困难",
        "special_groups": "elderly",
        "renal_hepatic_note": "肌酐清除率<50ml/min 需密切监测抗胆碱能不良反应",
        "review_points": "吸入胶囊是否被误作口服；青光眼与前列腺增生禁忌是否核对",
    },
    {
        "drug_code": "R05CB06",
        "max_daily_dose": 90,
        "dose_unit": "mg",
        "note": "祛痰药，不与强效镇咳药同用",
        "renal_hepatic_note": "重度肾功能不全者代谢产物蓄积，需减量",
        "review_points": "是否与中枢镇咳药联用致痰液潴留；疗程是否过长",
    },
    {
        "drug_code": "R06AX13",
        "max_daily_dose": 10,
        "dose_unit": "mg",
        "note": "第二代抗组胺药，嗜睡较轻",
        "special_groups": "child",
        "renal_hepatic_note": "严重肝功能不全者起始隔日给药；肾功能不全无需调整",
        "review_points": "儿童剂量是否按体重折算；是否与第一代抗组胺药重复",
    },
]
