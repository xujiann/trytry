"""运维类文档（运维手册 / README / 发布流程）写的部署与运维步骤照做得通（第五十三批「手册与规范写的 vs 现行行为」扫描 AQ4）。

这些文档是运维照着敲的命令，写错了不报错，只在照做的那一天出事。每条用例的 docstring 写条目号、扫描编号与修前的现象；
按纯文本扫文档，不起服务。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = (ROOT / "docs" / "运维手册.md").read_text(encoding="utf-8")
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
