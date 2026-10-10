"""运维与对接类文档（运维手册 / README / 发布流程 / 接口对接规范 …）写的步骤与口径照现行实现（第五十三批「手册与规范写的 vs
现行行为」扫描 AQ4）。

这些文档是运维、对接方照着敲的命令与报文，写错了不报错，只在照做的那一天出事。每条用例的 docstring 写条目号、扫描编号与
修前的现象；按纯文本扫文档、对照代码现取的口径，不起服务（补建唯一索引那条在临时 SQLite 上真跑迁移）。
"""
import ast
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
RUNBOOK = (ROOT / "docs" / "运维手册.md").read_text(encoding="utf-8")
RELEASE = (ROOT / "docs" / "发布流程.md").read_text(encoding="utf-8")
SPEC = (ROOT / "docs" / "接口对接规范.md").read_text(encoding="utf-8")
XINCHUANG = (ROOT / "docs" / "信创适配与备份容灾.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """从以 `heading` 开头的标题行起、到下一个标题行（任意级）为止的一段。代码块里 `# ` 开头的是 shell 注释，不算标题。"""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    assert start is not None, f"文档里找不到以「{heading}」开头的标题"
    out, in_code = [lines[start]], False
    for line in lines[start + 1:]:
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"#{1,6} ", line):
            break
        out.append(line)
    return "\n".join(out)


def _commands(section: str) -> list[str]:
    """这一段代码块里的每一条命令（去掉行尾 `#` 注释与整行注释）。"""
    commands, in_code = [], False
    for line in section.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        command = line.split("#", 1)[0].strip()
        if in_code and command:
            commands.append(command)
    return commands


def _compose_sections() -> list[tuple[str, str]]:
    return [("运维手册 §1.1", _section(RUNBOOK, "### 1.1 ")), ("README 生产部署", _section(README, "## 生产部署"))]


def test_compose部署段的变量覆盖编排文件的全部必填变量():
    """P2-1802（第五十三批扫描 AQ4-1）：运维手册 §1.1 标着「推荐」的 compose 部署只 export `MEDPLAT_SECRET`、
    `MEDPLAT_ADMIN_PASSWORD` 两个变量，README 的生产部署段一个都没写；`docker-compose.yml` 里 `MEDPLAT_DB_PASSWORD` 同样是
    `${…:?}` 必填，照做 compose 解析即报「required variable MEDPLAT_DB_PASSWORD is missing a value」退出。
    这里从编排文件现取全部 `${VAR:?…}`，以后编排文件加了必填变量、文档没跟就红。"""
    required = set(re.findall(r"\$\{(\w+):\?", COMPOSE))
    assert {"MEDPLAT_SECRET", "MEDPLAT_ADMIN_PASSWORD", "MEDPLAT_DB_PASSWORD"} <= required, required
    for name, section in _compose_sections():
        missing = sorted(var for var in required if var not in section)
        assert not missing, f"{name} 没写编排文件里的这些必填变量：{missing}"


def test_compose部署段不在宿主机跑alembic():
    """P2-1802（第五十三批扫描 AQ4-1）：两处原先都在 `docker compose up -d` 之后让人在宿主机 `cd server && alembic upgrade
    heads`。宿主机上没有容器里的 `MEDPLAT_*`，db 也不对宿主机发布端口，迁移落进 `server/` 下新建的一份空 SQLite（262 张表、
    exit 0），随后的 `scripts/backup.sh` 把这份空库当成生产库打包。迁移本来就由容器的 start.sh 先跑；修后两处只留一行
    进容器核对（`docker compose exec app alembic …`），这里钉住：部署段里的 alembic 命令一律进容器跑。"""
    for name, section in _compose_sections():
        commands = _commands(section)
        assert not [c for c in commands if re.search(r"cd server\s*&&.*\balembic\b", c)], f"{name} 又让人在宿主机跑迁移"
        alembic = [c for c in commands if re.search(r"\balembic\b", c)]
        assert alembic, f"{name} 找不到核对迁移的那一行，扫描对象变了？"
        assert all(c.startswith("docker compose exec app alembic ") for c in alembic), alembic


def _numbered_items(section: str) -> list[str]:
    """一段里的有序列表项（`1. …` 起头，缩进的续行并进上一项）。"""
    items: list[str] = []
    for line in section.splitlines():
        if re.match(r"\d+\. ", line):
            items.append(line)
        elif items and line.startswith("   ") and line.strip():
            items[-1] += line.strip()
    return items


def test_密钥轮换步骤在清PREVIOUS之前先做PII重加密():
    """P2-1803（第五十三批扫描 AQ4-2）：运维手册第九节「密钥轮换」原先四步只提 JWT、审计链、归档 MAC——设 PREVIOUS、换密钥、
    重启、宽限期后清空 PREVIOUS。PII 列加密开着时照做：旧钥加密的密文清空 PREVIOUS 后解不开、旧钥算的检索索引也对不上，
    患者清单 500、按证件号查不到、同证件号能再建一份档案；第十二节写了换钥后要跑 `pii_encrypt_backfill.py --old-secret`、
    跑完再清 PREVIOUS，第九节一句不指向它。这里钉住：轮换步骤里有 `--old-secret` 回填这一步，且排在清空 PREVIOUS 那一步
    之前；第九、十二两节互相指向。"""
    section = _section(RUNBOOK, "## 九、密钥轮换")
    steps = _numbered_items(section)
    backfill = [i for i, step in enumerate(steps) if "pii_encrypt_backfill.py --old-secret" in step]
    clear = [i for i, step in enumerate(steps) if "**清空** `MEDPLAT_SECRET_PREVIOUS`" in step]
    assert backfill and clear, f"轮换步骤里找不到回填或清空 PREVIOUS 那一步：{steps}"
    assert backfill[0] < clear[0], f"回填（第 {backfill[0] + 1} 步）要排在清空 PREVIOUS（第 {clear[0] + 1} 步）之前"
    assert "第十二节" in steps[backfill[0]], "回填那一步要指向第十二节（PII 列加密启用流程）"
    assert "第九节" in _section(RUNBOOK, "## 十二、PII 列加密启用流程"), "第十二节的轮换说明要指回第九节的步骤"


def test_回滚只回代码时跳过启动期迁移_降库先用新版再换旧代码():
    """P2-1804（第五十三批扫描 AQ4-3）：发布流程写「回退镜像、只回代码不回库」是首选，运维手册规程 2 写「部署旧代码 →
    alembic downgrade」。可只要新版带了迁移，库里就记着旧代码不认识的 revision：旧镜像的 start.sh 先跑 `alembic upgrade
    heads`，报 Can't locate revision、`set -e` 退出（compose 下无限重启）；手册的顺序在旧代码上 downgrade，同样找不到新迁移
    文件、exit 255。这里钉住：手册规程 2 与发布流程的「回退镜像」一步写明那次部署设 `MEDPLAT_MIGRATE_ON_START=0`；要降库时
    先用新版代码（镜像）downgrade、再换旧代码。"""
    rollback = next(s for s in _numbered_items(_section(RUNBOOK, "## 三、Alembic 迁移操作")) if s.startswith("2. 回滚"))
    assert "MEDPLAT_MIGRATE_ON_START=0" in rollback, rollback
    assert "降库" in rollback, rollback
    downgrade_part = rollback[rollback.index("降库"):]
    assert "新版" in downgrade_part and downgrade_part.index("alembic downgrade") < downgrade_part.index("部署旧代码"), (
        "规程 2 要先用新版代码 downgrade、再部署旧代码：" + rollback)
    assert "MEDPLAT_MIGRATE_ON_START=0" in _section(RELEASE, "### 1. 回退镜像 tag")
    downgrade = _section(RELEASE, "### 2. `alembic downgrade`")
    assert "用新版镜像" in downgrade and "换旧镜像" in downgrade, downgrade
    assert downgrade.index("用新版镜像") < downgrade.index("换旧镜像"), downgrade


#: 按单 head 写的 alembic 命令：单数 head 的 stamp / upgrade（两个 head 下报 Multiple heads），不限定链的相对 downgrade
_SINGLE_HEAD_COMMAND = re.compile(r"\balembic\s+(?:stamp|upgrade)\s+head\b|\balembic\s+downgrade\s+-\d")


def test_alembic命令按平台链加spd链两个head写():
    """P2-1806（第五十三批扫描 AQ4-6）：运维手册的 alembic 命令按单 head 写——第三节 `alembic downgrade -1`「回退一个版本」，
    两条链时降哪条不定（实测降的是 spd 链 1b10d2f72426、删了 `spd_package_bindings.period_days`，这一版的平台迁移反而还在，
    alembic 只给 UserWarning、exit 0）；1.4、第三节、FAQ 三处 `alembic stamp head` 直接报 Multiple heads、exit 255。发布流程
    早写明 downgrade 按链、先看 heads。这里扫运维手册、发布流程与 README，单数 head 的 stamp / upgrade 与不限定链的相对
    downgrade 出现即红（平台链写目标 revision，spd 链用 `spd@-1`）。"""
    offenders = [f"{name}：{line.strip()}"
                 for name, text in (("运维手册", RUNBOOK), ("发布流程", RELEASE), ("README", README))
                 for line in text.splitlines() if _SINGLE_HEAD_COMMAND.search(line)]
    assert not offenders, "这些 alembic 命令按单 head 写，两个 head 下照做会失败或降错链：\n" + "\n".join(offenders)
    assert "alembic downgrade spd@-1" in RUNBOOK, "第三节要给出 spd 链的回退写法"


DEDUP_MIGRATION = SERVER / "alembic" / "versions" / "e1a2b3c4d5e9_spd十八张表补唯一索引与去重.py"
#: 补建唯一索引那几句里认的语句（核对用的 SELECT pg_indexes 是 PostgreSQL 的，不在 SQLite 上跑）
_REBUILD_VERBS = ("BEGIN", "DROP", "CREATE", "COMMIT")


def _sql_statements(block: str) -> list[str]:
    body = "\n".join(line for line in block.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip().split(" ", 1)[0].upper() in _REBUILD_VERBS]


def _manual_rebuild_sql() -> list[str]:
    """运维手册第十五节「不用脚本、手工处置的」后面第一个 sql 代码块。"""
    tail = RUNBOOK[RUNBOOK.index("**不用脚本、手工处置的**"):]
    return _sql_statements(tail.split("```sql", 1)[1].split("```", 1)[0])


def _docstring_rebuild_sql() -> list[str]:
    """迁移 e1a2b3c4d5e9 docstring 里的人工处置 SQL（迁移的 ERROR 日志指向这里）：「不想用脚本」那段到 downgrade 一节之间
    缩进的那几行。"""
    doc = ast.get_docstring(ast.parse(DEDUP_MIGRATION.read_text(encoding="utf-8"))) or ""
    part = doc[doc.index("不想用脚本"):doc.index("## downgrade")]
    return _sql_statements("\n".join(line for line in part.splitlines() if line.startswith("    ")))


def _alembic(db: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "MEDPLAT_DATABASE_URL"}
    proc = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=SERVER, capture_output=True, text=True,
                          env={**env, "MEDPLAT_DATABASE_URL": f"sqlite:///{db}"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def _connect(db: Path) -> sqlite3.Connection:
    """裸连接、自动提交（语句里自带 BEGIN / COMMIT）；不开外键——只造重复键，user_id 不必真有其人。"""
    return sqlite3.connect(db, isolation_level=None)


def _unique_flag(db: Path, table: str, index: str) -> int | None:
    with closing(_connect(db)) as con:
        return next((row[2] for row in con.execute(f"PRAGMA index_list({table})") if row[1] == index), None)


def test_手工补建唯一索引的SQL照抄能建出唯一索引(tmp_path):
    """P2-1805（第五十三批扫描 AQ4-5）：迁移 e1a2b3c4d5e9 探到存量重复就跳过那张表的唯一索引，原样留着同名的普通索引 `ix_*`；
    运维手册第十五节「不用脚本、手工处置的」与迁移 docstring（迁移的 ERROR 日志指向它）给的却是直接
    `CREATE UNIQUE INDEX ix_spd_point_accounts_user_id …`——按台账删掉重复行后照抄，报「index … already exists」，DBA 容易
    读成「已经在了」就收工，唯一性仍缺着（积分账户和病种两类重复脚本并不掉，只能走这条手工路，P2-1101）。

    照扫描 r7 的路子：库停在 e1a2b3c4d5e9 之前造一个村医两个积分账户 → `upgrade heads`（迁移跳过、落台账）→ 按台账删掉
    重复行 → 把手册与迁移 docstring 里的补建 SQL 各在一份库副本上照抄执行，都得建出唯一索引。"""
    db = tmp_path / "dedup.db"
    _alembic(db, "upgrade", "d8e9f1a2b3c8")   # spd 链停在本迁移之前
    with closing(_connect(db)) as con:
        columns = [row for row in con.execute("PRAGMA table_info(spd_point_accounts)") if row[1] != "id"]
        values = [999 if c[1] == "user_id" else ("2026-01-01 00:00:00" if "DATE" in (c[2] or "").upper() else 0)
                  for c in columns]
        insert = (f"INSERT INTO spd_point_accounts ({', '.join(c[1] for c in columns)}) "
                  f"VALUES ({', '.join('?' for _ in columns)})")
        con.execute(insert, values)
        con.execute(insert, values)
    log = _alembic(db, "upgrade", "heads")
    assert "跳过建唯一索引 ix_spd_point_accounts_user_id" in log, "前提：迁移探到重复、跳过了这张表"
    assert _unique_flag(db, "spd_point_accounts", "ix_spd_point_accounts_user_id") == 0, "前提：留着同名普通索引"
    with closing(_connect(db)) as con:   # 按台账处置：删掉被并掉的那一行
        (removed,) = con.execute("SELECT removed_id FROM spd_dedup_reports "
                                 "WHERE table_name = 'spd_point_accounts' AND strategy = 'pending'").fetchone()
        con.execute("DELETE FROM spd_point_accounts WHERE id = ?", (removed,))

    for name, statements in (("运维手册第十五节", _manual_rebuild_sql()), ("迁移 docstring", _docstring_rebuild_sql())):
        assert any(s.upper().startswith("CREATE UNIQUE INDEX") for s in statements), f"{name} 找不到补建语句：{statements}"
        copy = tmp_path / f"{name}.db"
        shutil.copy(db, copy)
        with closing(_connect(copy)) as con:
            for statement in statements:
                con.execute(statement)   # 修前这里报 OperationalError: index … already exists
        assert _unique_flag(copy, "spd_point_accounts", "ix_spd_point_accounts_user_id") == 1, f"{name} 的 SQL 没建出唯一索引"


def test_对接规范列全对接适配层的接口与所需角色():
    """P2-1809（第五十三批扫描 AQ4-9）：接口对接规范附录A 前言写「未列出的 GET 类接口仅要求登录」，对 `/api/integration/*`
    不成立——角色守卫挂在整个路由器上（`require_roles("operator")`），GET 也在内：医师 `GET /api/integration/fhir/Patient/{ehc}`
    403「需要以下角色之一：经办人员」，公卫推 FHIR Observation 403（同样的随访走 `/api/chronic/{id}/followups` 是 201）；
    入站端点的路径、请求体、所需角色规范里一处没写。这里从 `integration.py` 的路由表现取：每个接口都在规范第六节出现，
    路由器层守卫要的角色写在那一节，附录A 前言不再说 GET 只要登录。"""
    from fastapi.routing import APIRoute

    from app.deps import ROLE_NAMES
    from app.routers import integration

    section = _section(SPEC, "## 六、对接适配层")
    routes = [route for route in integration.router.routes if isinstance(route, APIRoute)]
    assert len(routes) >= 9, f"integration 路由表只取到 {len(routes)} 个接口，扫描对象变了？"
    missing = sorted(f"{method} {route.path}" for route in routes for method in route.methods
                     if f"`{method} {route.path}`" not in section)
    assert not missing, f"对接规范第六节没写这些对接适配层接口：{missing}"
    roles = [role for dep in integration.router.dependencies
             for role in inspect.getclosurevars(dep.dependency).nonlocals.get("roles", ())]
    assert roles, "integration 路由器上找不到角色守卫，扫描对象变了？"
    required = re.search(r"^- \*\*所需角色\*\*.*?(?=^- |\Z)", section, re.M | re.S)
    assert required, f"规范第六节没有「所需角色」一条：{section[:300]}"
    unwritten = [role for role in roles if role not in required.group() or ROLE_NAMES[role] not in required.group()]
    assert not unwritten, f"规范第六节「所需角色」没写对接适配层要的角色：{unwritten}"
    preface = SPEC[SPEC.index("## 附录A"):].split("\n\n", 2)[1]
    assert "仅要求登录" not in preface and "`/api/integration/*`" in preface, preface


def test_对接规范写的debug_code回显条件与代码一致():
    """P2-1810（第五十三批扫描 AQ4-10）：接口对接规范附录C 写 `debug_code`「仅当短信通道为 console 且 environment != prod
    时回显」，少了 `MEDPLAT_SMS_DEBUG_ECHO`——`portal.py` 要三个条件同时满足（P0-4 只改了代码，规范没跟；README 与运维手册
    已写三条件）。照规范只配两条去联调：`200 {'sent': True, …}`、没有 debug_code，console 通道的日志只打掩码号码，验证码
    无处可取。这里从 `portal.py` 给 `debug_code` 赋值的那个 if 现取条件，规范那一条要写全：console、开关变量名、非生产。"""
    from app.routers import portal

    guard = next(node.test for node in ast.walk(ast.parse(inspect.getsource(portal))) if isinstance(node, ast.If)
                 and any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                         and target.slice.value == "debug_code"
                         for stmt in node.body if isinstance(stmt, ast.Assign) for target in stmt.targets))
    attrs = {node.attr for node in ast.walk(guard) if isinstance(node, ast.Attribute)}
    consts = {node.value for node in ast.walk(guard) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {"sms_debug_echo", "is_production"} <= attrs and "console" in consts, ast.unparse(guard)
    appendix = _section(SPEC, "### 附录C")
    bullet = re.search(r"^- \*\*debug_code\*\*.*?(?=^- |\Z)", appendix, re.M | re.S)
    assert bullet, "附录C 找不到 debug_code 那一条"
    missing = [need for need in ("`console`", "MEDPLAT_SMS_DEBUG_ECHO", "prod") if need not in bullet.group()]
    assert not missing, f"附录C 的 debug_code 回显条件少写了：{missing}"


def _sentences(text: str) -> list[str]:
    """按句切：段内的硬换行先接起来（空行、列表项、标题仍是边界），再按句号、分号、叹号、问号切。"""
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |\d+\. |#))", text)
    return [s.strip() for block in blocks for s in re.split(r"[。；！？]", re.sub(r"\n\s*", "", block)) if s.strip()]


def test_信创文档写明SM4已用于PII列加密():
    """P2-1811（第五十三批扫描 AQ4-11）：《信创适配与备份容灾》2.3 写「SM2/SM4 平台侧不做实现」、第四节写「不自己实现
    SM2/SM4」，可 SM4 早已用于 PII 列加密——`app/gmcrypto.py` 明写「SM4 同样是纯 Python 实现」，`app/pii.py` 的密文就是
    SM4-CTR。照这份文档写的等保 / 密评材料会与实际不符。这里钉住：PII 加密确实走 `gmcrypto.sm4_ctr`（前提），文档里不再有
    「SM4」与「不做 / 不实现」同句，并写明 SM4 用在 PII 列加密。"""
    from app import pii

    assert "gmcrypto.sm4_ctr" in inspect.getsource(pii), "前提：PII 列加密用的是 gmcrypto 的 SM4"
    sentences = _sentences(XINCHUANG)
    wrong = [s for s in sentences if "SM4" in s and re.search(r"不做|不实现|不自己实现", s)]
    assert not wrong, f"文档还说 SM4 平台侧不实现：{wrong}"
    assert any("SM4" in s and "PII" in s for s in sentences), "文档要写明 SM4 用于 PII 列加密"
