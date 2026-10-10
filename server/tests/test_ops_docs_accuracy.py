"""运维类文档（运维手册 / README / 发布流程）写的部署与运维步骤照做得通（第五十三批「手册与规范写的 vs 现行行为」扫描 AQ4）。

这些文档是运维照着敲的命令，写错了不报错，只在照做的那一天出事。每条用例的 docstring 写条目号、扫描编号与修前的现象；
按纯文本扫文档，不起服务。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = (ROOT / "docs" / "运维手册.md").read_text(encoding="utf-8")
RELEASE = (ROOT / "docs" / "发布流程.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """从以 `heading` 开头的标题行起、到下一个标题行（任意级）为止的一段。代码块里 `# ` 开头的是 shell 注释，不算标题。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
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
