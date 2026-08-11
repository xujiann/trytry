"""会计科目种子（T3.1）：医院会计制度常用一级科目。

只放一级科目——明细科目各院口径差异大，交由管理员按需增建。
direction 是余额方向：资产/费用借增，负债/净资产/收入贷增。
"""

SEED_ACCOUNT_SUBJECTS = [
    # 资产
    {"code": "1001", "name": "库存现金", "category": "asset", "direction": "debit"},
    {"code": "1002", "name": "银行存款", "category": "asset", "direction": "debit"},
    {"code": "1101", "name": "应收医疗款", "category": "asset", "direction": "debit"},
    {"code": "1102", "name": "应收在院病人医疗款", "category": "asset", "direction": "debit"},
    {"code": "1201", "name": "库存物资", "category": "asset", "direction": "debit"},
    {"code": "1301", "name": "固定资产", "category": "asset", "direction": "debit"},
    {"code": "1302", "name": "累计折旧", "category": "asset", "direction": "credit"},
    # 负债
    {"code": "2001", "name": "短期借款", "category": "liability", "direction": "credit"},
    {"code": "2101", "name": "应付账款", "category": "liability", "direction": "credit"},
    {"code": "2201", "name": "应付职工薪酬", "category": "liability", "direction": "credit"},
    {"code": "2301", "name": "预收医疗款", "category": "liability", "direction": "credit"},
    # 净资产
    {"code": "3001", "name": "事业基金", "category": "net_asset", "direction": "credit"},
    {"code": "3101", "name": "专用基金", "category": "net_asset", "direction": "credit"},
    {"code": "3201", "name": "财政补助结转", "category": "net_asset", "direction": "credit"},
    # 收入
    {"code": "4001", "name": "医疗收入", "category": "income", "direction": "credit"},
    {"code": "4002", "name": "财政补助收入", "category": "income", "direction": "credit"},
    {"code": "4003", "name": "科教项目收入", "category": "income", "direction": "credit"},
    {"code": "4004", "name": "其他收入", "category": "income", "direction": "credit"},
    # 费用
    {"code": "5001", "name": "医疗业务成本", "category": "expense", "direction": "debit"},
    {"code": "5002", "name": "财政项目补助支出", "category": "expense", "direction": "debit"},
    {"code": "5101", "name": "管理费用", "category": "expense", "direction": "debit"},
    {"code": "5201", "name": "其他支出", "category": "expense", "direction": "debit"},
]
