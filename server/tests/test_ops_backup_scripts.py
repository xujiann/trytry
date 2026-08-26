"""主线备份/恢复脚本的最低保障：`backup.sh` / `restore.sh` / `restore_drill.sh`。

**为什么这个文件必须存在**：这三个脚本是灾难恢复路径上仅有的可执行物（运维手册
第四节点名的就是它们仨），上线前审计发现它们**零测试覆盖**——同目录的
`test_ops_wal_pitr_scripts.py` 只覆盖了 PG 的 PITR 那两个，CI 里也没有任何
shellcheck/备份步骤。改坏了没人会知道，而"备份能不能用，只有恢复的时候才知道，
而那时候来不及了"（`restore_drill.sh` 自己的开头语）。

覆盖面（如实列出，不假装更多）：

1. 三个脚本 `bash -n` 语法合法 —— 此前连这条最低线都没有；
2. SQLite 全链真跑：`backup.sh` → 破坏现场 → `restore.sh` → 数据与附件都回来了；
3. 备份包被掉包（内容换过、包结构完好）后 `restore.sh` 拒绝恢复，且**不动**现有库
   ——`.sha256` 的完整性校验必须是真的；
4. `restore_drill.sh` 钉住 `alembic_version` **恰有两行**（平台链 + spd 链）——
   只有一行说明备份来自 `alembic upgrade head`（单数），spd 的 59 张表根本没进去；
5. `restore_drill.sh` 遇 PG 备份包明确 exit 2，不假装演练成功；
6. `backup.sh` 不会给一个不存在的库"备"出空包；密钥指纹不符时 `restore.sh` 停下来问。

**防假绿（本仓库反复吃过"守卫写错→用例恒绿"的亏）**：每条正向断言都配一个反向锚点。
最要命的一条是"恢复后数据一致"——在两个空库之间它同样成立，什么都没证明。故本文件
一律先断言"库里真写进了可辨识的标记行"、再断言"破坏真的生效了（读出来是被破坏的值）"，
最后才断言"恢复后读回的是备份时那一份"。被拒的路径同理：拒绝之后现场必须原样没动。

**未覆盖，如实说明**：PG 分支（需 `pg_dump`/`pg_restore`，另见 `restore_drill_pg.sh`
与 `test_ops_wal_pitr_scripts.py`）；`.sha256` 旁文件**缺失**时三个脚本干脆不做完整性
校验这一既有缺口（当前行为，不在此处固化为"期望"）。

速度：全文件约 3~4 秒，可进 `make test-unit`。合成库代替真库是关键——真库 258 张表要
跑 86 个迁移才建得出来，而演练脚本的门槛只有"表数 > 100 / users+organizations 非空 /
alembic_version 两行"，合成库把这三条如实复现，代价是毫秒级。
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SERVER_DIR / "scripts"

#: 运维手册第四节点名的主线三个脚本（PG/PITR 那两个由 test_ops_wal_pitr_scripts.py 覆盖）。
MAIN_SCRIPTS = ("backup.sh", "restore.sh", "restore_drill.sh")

#: 合成库的填充表数量。`restore_drill.sh` 判 `tables <= 100` 即"备份可能不完整"，
#: 这里取 105 张填充表 + users/organizations/alembic_version = 108 张，刚好过线。
_FILLER_TABLES = 105

_REQUIRED_TOOLS = ("bash", "tar", "gzip", "sha256sum", "python3", "alembic")
_MISSING_TOOLS = [name for name in _REQUIRED_TOOLS if shutil.which(name) is None]

#: 环境不具备时**明确说出缺什么**，不静默跳过——"跳过了"和"通过了"在 CI 摘要里长得太像。
requires_shell_tools = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"备份脚本运行所需命令缺失：{', '.join(_MISSING_TOOLS)}；装上后本组用例自动恢复",
)
requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="缺少 bash，无法做脚本语法检查"
)


# ---------------------------------------------------------------- 工具函数


def _script_env(*, db_path: Path, upload_dir: Path, secret: str) -> dict:
    """构造脚本运行环境，并**强制**把它指向临时目录。

    这不是可有可无的谨慎：`tests/conftest.py` 在进程环境里设了
    `MEDPLAT_DATABASE_URL=sqlite:///./test_run.db` 与 `MEDPLAT_UPLOAD_DIR=./test_uploads`，
    子进程原样继承；而 `restore.sh` 会**覆盖** `MEDPLAT_DATABASE_URL` 指向的库文件。
    漏掉这层覆盖，本文件就会把整个测试套件共用的 test_run.db 冲掉，且是在别的用例里报错。
    所以这里不只是"设值"，还断言值确实落在仓库之外。
    """
    db_path = db_path.resolve()
    upload_dir = upload_dir.resolve()
    for path in (db_path, upload_dir):
        assert path.is_absolute(), f"脚本只接受绝对路径，避免解析到脚本自己的 cwd：{path}"
        assert SERVER_DIR not in path.parents and path != SERVER_DIR, (
            f"拒绝把备份/恢复脚本指向仓库内的路径（会毁掉 test_run.db 等共用文件）：{path}"
        )
    env = dict(os.environ)
    # 绝对路径的 SQLAlchemy SQLite 写法是四条斜杠（sqlite:/// + /abs/path）；
    # 脚本用 `${DB_URL#sqlite:///}` 取回路径，两边口径一致。
    env["MEDPLAT_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["MEDPLAT_UPLOAD_DIR"] = str(upload_dir)
    env["MEDPLAT_SECRET"] = secret
    return env


def _run_script(name: str, *args: str, env: dict | None = None, cwd: Path | None = None):
    """跑脚本。cwd 一律给临时目录：脚本里所有相对默认值（如 `./backups`）都不该落进仓库。"""
    return subprocess.run(
        ["bash", str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _build_synthetic_db(db_path: Path, *, heads, marker: str) -> None:
    """造一个"长得像 medplat 生产库"的最小合成库。

    `marker` 会写进 users/organizations，用于证明"恢复回来的确实是备份的那一份"，
    而不是恰好两边都空。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (username) VALUES (?)", (marker,))
        conn.execute("INSERT INTO organizations (name) VALUES (?)", (f"org-{marker}",))
        conn.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        conn.executemany(
            "INSERT INTO alembic_version VALUES (?)", [(head,) for head in heads]
        )
        for index in range(_FILLER_TABLES):
            conn.execute(f"CREATE TABLE filler_{index} (id INTEGER)")
        conn.commit()
    finally:
        conn.close()


def _query(db_path: Path, sql: str):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _usernames(db_path: Path) -> list:
    """读 users 的标记行。库里连 users 表都没有时给一句人话，而不是抛 OperationalError——
    "备出来的是个空库"是这里最可能的真实故障，报错该一眼能看懂。"""
    tables = {row[0] for row in _query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "users" in tables, f"{db_path.name} 里没有 users 表，看上去是个空库：现有表 {sorted(tables)}"
    return [row[0] for row in _query(db_path, "SELECT username FROM users ORDER BY id")]


def _sidecar(archive: Path) -> Path:
    """备份包的 .sha256 旁文件（backup.sh 里是 `$ARCHIVE.sha256`，整名后缀而非替换扩展名）。"""
    return archive.with_name(archive.name + ".sha256")


def _read_manifest(archive: Path) -> dict:
    """从备份包里读出 manifest.txt（backup.sh 打包时成员名是 ./manifest.txt）。"""
    return dict(
        line.split("=", 1)
        for line in _member_bytes(archive, "manifest.txt").decode("utf-8").splitlines()
        if "=" in line
    )


def _member_bytes(archive: Path, basename: str) -> bytes:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if Path(member.name).name == basename:
                handle = tar.extractfile(member)
                assert handle is not None, f"{basename} 不是普通文件"
                return handle.read()
    raise AssertionError(f"备份包里没有 {basename}：{[m.name for m in tarfile.open(archive).getmembers()]}")


def _make_backup(tmp: Path, *, heads, marker: str, attachment: bytes | None = b"attachment-payload"):
    """造库 → 跑 backup.sh → 返回 (备份包, 源库, 附件目录, 环境)。"""
    db_path = tmp / "app.db"
    upload_dir = tmp / "uploads"  # basename 固定为 uploads，便于断言 manifest
    upload_dir.mkdir()
    if attachment is not None:
        (upload_dir / "report.pdf").write_bytes(attachment)
    _build_synthetic_db(db_path, heads=heads, marker=marker)

    out_dir = tmp / "backups"
    env = _script_env(db_path=db_path, upload_dir=upload_dir, secret=f"secret-{marker}")
    proc = _run_script("backup.sh", str(out_dir), env=env, cwd=tmp)
    assert proc.returncode == 0, f"backup.sh 失败：\n{proc.stdout}\n{proc.stderr}"

    archives = sorted(out_dir.glob("medplat-backup-*.tar.gz"))
    assert len(archives) == 1, f"备份包数量异常：{archives}"
    assert _sidecar(archives[0]).exists(), "备份包缺少 .sha256 校验文件"
    return archives[0], db_path, upload_dir, env


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def alembic_heads():
    """当前代码的全部 alembic head。本仓库是双分支迁移，应恰有 2 个（平台链 + spd 链）。

    不硬编码 revision id：加一条迁移就会变，硬编码等于把这个文件变成每次迁移都要改的负担。
    读脚本目录（不连库、不跑 env.py），约 0.3 秒。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = sorted(ScriptDirectory.from_config(Config(str(SERVER_DIR / "alembic.ini"))).get_heads())
    assert len(heads) == 2, (
        f"当前代码有 {len(heads)} 个 alembic head：{heads}；本仓库应为 2 个（平台链 + spd 链）。"
        " 本文件第 4 条用例（演练脚本钉住两行）以此为前提，先修迁移图再回来看这条。"
    )
    return heads


@pytest.fixture(scope="module")
def dual_head_backup(tmp_path_factory, alembic_heads):
    """一个双 head 的正常备份包（只读复用：多条用例都拿它当输入，谁也不改它）。"""
    tmp = tmp_path_factory.mktemp("backup-dual")
    archive, _db, _uploads, env = _make_backup(tmp, heads=alembic_heads, marker="双head现场")
    return archive, env["MEDPLAT_SECRET"]


# ---------------------------------------------------------------- 1. 语法


@requires_bash
def test_三个主线脚本的bash语法合法():
    """最低线：这三个脚本此前连语法检查都没有——写错一个引号，要到灾难当天才发现。

    用 `bash -n` 而不是 `sh -n`：脚本第 11 行是 `set -euo pipefail`，`pipefail` 是
    bash/ksh 扩展。dash 能**解析**它、却不能**执行**（"Illegal option -o pipefail"），
    所以 `sh -n` 在这里恰好是一种假绿：语法检查过了，脚本在 Debian 系（/bin/sh -> dash）
    上根本跑不起来。
    """
    for name in MAIN_SCRIPTS:
        script = SCRIPTS / name
        assert script.exists(), f"运维手册第四节点名的脚本不见了：{script}"
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{name} 语法错误：{proc.stderr}"


def test_三个主线脚本声明bash解释器与严格模式():
    """shebang 必须是 bash，且保留 `set -euo pipefail`——两者是一对，拆开哪个都会出事。

    去掉 `pipefail`：管道中段失败被吞，`tar | ...` 半截失败也照样 exit 0，"备份成功"是假的。
    把 shebang 改成 `/bin/sh`：Debian 系上第 11 行就 "Illegal option" 退出，而
    "备份目录里没有新文件"通常几周后才会被人注意到。
    """
    for name in MAIN_SCRIPTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        assert "bash" in first_line, f"{name} 的 shebang 不是 bash（{first_line}），但它用了 bash 扩展"
        assert "set -euo pipefail" in text, f"{name} 丢了严格模式，失败会被静默吞掉"


# ---------------------------------------------------------------- 2. 全链真跑


@requires_shell_tools
def test_sqlite全链_备份后破坏现场_restore能把数据与附件原样恢复(tmp_path, alembic_heads):
    """备份与恢复只有连起来跑通才算数：分开各测各的，最常见的失败恰恰在接缝上。

    本容器没有 `sqlite3` CLI（`which sqlite3` 为空），而 `backup.sh:36-46` 正是为此
    改走 Python 的 `sqlite3.backup()` API——这条路径在这里刚好可真跑，不必造假。

    防假绿三步（缺一条这个用例就可能在两个空库之间"通过"）：
      A. 备份前断言源库里真有可辨识的标记行、附件真有内容；
      B. 备份后**真的破坏现场**（改库 + 删附件），并断言破坏确实生效；
      C. 恢复后读回的必须是标记行、且附件字节完全相同。
    另外顺带钉住：备份包里那份 database.sqlite 自己就带着数据（"备份一直在跑、
    里面什么都没有"是这个脚本注释里点名过的历史事故）。
    """
    marker = "备份时的原始数据"
    attachment = "附件内容-不可损坏\n".encode("utf-8")
    archive, db_path, upload_dir, env = _make_backup(
        tmp_path, heads=alembic_heads, marker=marker, attachment=attachment
    )
    attachment_path = upload_dir / "report.pdf"

    # --- 防假绿 A：源库/附件在备份前确实非空 ---
    assert _usernames(db_path) == [marker], "源库没有标记行，后面的'恢复后一致'将毫无意义"
    assert attachment_path.read_bytes() == attachment

    # 清单如实记录双 head：backup.sh 记的是**当前代码**的 heads（不是库里的行）
    manifest = _read_manifest(archive)
    assert manifest["db_url_scheme"] == "sqlite"
    assert manifest["upload_dir_basename"] == "uploads"
    assert set(manifest["alembic_heads"].split(",")) == set(alembic_heads)
    assert manifest["alembic_head_count"] == "2", (
        "清单只记了一个 head：`alembic heads | head -1` 那个老 bug 回来了，"
        "spd 链的备份会看上去齐全、实则从未被核对"
    )

    # 备份包里那份库自己就得有数据（不是等恢复时才发现是空的）
    packed_db = tmp_path / "packed.sqlite"
    packed_db.write_bytes(_member_bytes(archive, "database.sqlite"))
    assert _usernames(packed_db) == [marker], "备份包里的库是空的——备份跑了个寂寞"

    # --- 防假绿 B：真的破坏现场 ---
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM users")
    conn.execute("INSERT INTO users (username) VALUES ('现场已被破坏')")
    conn.commit()
    conn.close()
    attachment_path.unlink()
    assert _usernames(db_path) == ["现场已被破坏"], "破坏没生效，下面的恢复就证明不了任何事"
    assert not attachment_path.exists()

    # --- 恢复 ---
    proc = _run_script("restore.sh", str(archive), env=env, cwd=tmp_path)
    assert proc.returncode == 0, f"restore.sh 失败：\n{proc.stdout}\n{proc.stderr}"
    assert "校验通过" in proc.stdout, "跳过了完整性校验？"

    # --- 防假绿 C：数据与附件都回到备份时那一份 ---
    assert _usernames(db_path) == [marker]
    assert _query(db_path, "SELECT name FROM organizations") == [(f"org-{marker}",)]
    assert attachment_path.read_bytes() == attachment, "附件没恢复或内容不一致"
    assert "1 个文件" in proc.stdout

    # 恢复不是"覆盖了事"：旧库被留成 .before-restore-*，出错还有回头路
    before = sorted(db_path.parent.glob("app.db.before-restore-*"))
    assert len(before) == 1, f"恢复前的现场没有留档：{before}"
    assert _usernames(before[0]) == ["现场已被破坏"], "留档的不是被覆盖的那一份"

    # 恢复末尾的 `alembic upgrade heads`（复数）没把双 head 弄丢
    assert len(_query(db_path, "SELECT version_num FROM alembic_version")) == 2


# ---------------------------------------------------------------- 3. 完整性校验


@requires_shell_tools
def test_备份包被掉包后restore拒绝恢复且不碰现有库(tmp_path, dual_head_backup, alembic_heads):
    """备份最该有的性质：**内容对不上校验和的包必须被拒**，而不是把别人的库糊到生产上。

    这里造的是**掉包**而不是"往包尾追加垃圾字节"：后者 tar 自己就会报错，验的是 tar
    而不是 sha256。掉包（包结构完好、内容被换过）才是 `.sha256` 唯一能挡住的东西，
    也正是脚本报错里说的"包已损坏或**被替换**"。

    防假绿两处：
      1. 同样这串字节，只把 .sha256 换成与它匹配的那一份，演练立刻就通过了——
         证明拦下它的确实是校验和，而不是包本身有毛病；
      2. 断言拒绝之后现场库还是**自己的**标记行——校验若发生在覆盖之后，等于没有校验。
    """
    archive, secret = dual_head_backup
    original_digest = _sidecar(archive).read_text(encoding="utf-8").split()[0]

    # 解开原包 → 换掉里面的库 → 原样打回去（包结构完好，tar 能正常解开）
    stage = tmp_path / "stage"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(stage, filter="data")
    (stage / "database.sqlite").unlink()
    _build_synthetic_db(stage / "database.sqlite", heads=alembic_heads, marker="掉包塞进来的库")
    swapped = tmp_path / "medplat-backup-swapped.tar.gz"
    with tarfile.open(swapped, "w:gz") as tar:
        tar.add(stage, arcname=".")
    # 旁文件仍写着**原包**的哈希（.sha256 里只记 basename，故换个文件名也该对得上）
    _sidecar(swapped).write_text(f"{original_digest}  {swapped.name}\n", encoding="utf-8")

    live_db = tmp_path / "live.db"
    live_upload = tmp_path / "uploads"
    live_upload.mkdir()
    _build_synthetic_db(live_db, heads=alembic_heads, marker="生产现场不许动")
    env = _script_env(db_path=live_db, upload_dir=live_upload, secret=secret)

    proc = _run_script("restore.sh", str(swapped), env=env, cwd=tmp_path)
    assert proc.returncode == 4, f"掉包竟然没被拒：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "备份包校验失败" in proc.stderr
    assert "恢复中止" in proc.stderr
    assert _usernames(live_db) == ["生产现场不许动"], "现场库已被掉包内容覆盖——校验来得太晚"
    assert not list(tmp_path.glob("live.db.before-restore-*")), "已经动过现场库了"

    # 演练脚本必须同口径拒绝：演练放行、正式恢复才拦，等于演练没演到点子上
    drill = _run_script("restore_drill.sh", str(swapped), env=env, cwd=tmp_path)
    assert drill.returncode == 4, f"restore_drill.sh 放过了掉包：{drill.stdout}\n{drill.stderr}"
    assert "备份包校验失败" in drill.stderr

    # 防假绿：同样的字节 + 与之匹配的 .sha256 → 演练通过。上面的拒绝确实来自校验和。
    matching = hashlib.sha256(swapped.read_bytes()).hexdigest()
    _sidecar(swapped).write_text(f"{matching}  {swapped.name}\n", encoding="utf-8")
    ok = _run_script("restore_drill.sh", str(swapped), env=env, cwd=tmp_path)
    assert ok.returncode == 0, f"包本身就是坏的，上面的 exit 4 说明不了校验和有用：\n{ok.stdout}\n{ok.stderr}"


# ---------------------------------------------------------------- 4. 双 head


@requires_shell_tools
def test_演练脚本要求alembic_version恰有两行(dual_head_backup):
    """正向：双 head 的备份包演练通过，且演练确实"看过"了 head 行数。

    这是 `restore_drill.sh:67-76` 的核心语义：本仓库双分支迁移，`alembic_version`
    应当恰有两行。少一行就是 spd 链没进备份。
    """
    archive, _secret = dual_head_backup
    proc = _run_script("restore_drill.sh", str(archive))
    assert proc.returncode == 0, f"正常备份包演练失败：\n{proc.stdout}\n{proc.stderr}"
    assert "head 行数：2（预期 2）" in proc.stdout, f"演练没检查 head 行数：\n{proc.stdout}"
    assert "演练通过" in proc.stdout


@requires_shell_tools
def test_演练脚本拒绝只有单head的备份包(tmp_path, alembic_heads):
    """反向：**只有一行**就说明备份来自 `alembic upgrade head`（单数），spd 的 59 张表没进去。

    这条是本文件里最不能省的一条。它反的是 `fetchone()` 那类"看一眼就下结论"的检查：
    单 head 的备份表数正常、users/organizations 都有数据、迁移版本也"可识别"，
    前三步全绿——只有第四步的"恰两行"能把它拦下来。

    防假绿：这个包与上一条用例的包**只差 alembic_version 的行数**（同样的表数、
    同样的 users/organizations 行），所以转红只可能来自第四步。
    """
    archive, _db, _uploads, _env = _make_backup(
        tmp_path, heads=[alembic_heads[0]], marker="单head现场"
    )
    proc = _run_script("restore_drill.sh", str(archive))

    assert proc.returncode != 0, f"单 head 备份包竟然演练通过了：\n{proc.stdout}"
    # 前三步应当都过了——证明拦下它的确实是"双 head"这一步，而不是别的什么先炸了
    assert "[3/4] 迁移版本可识别" in proc.stdout
    assert "head 行数：1（预期 2）" in proc.stdout
    assert "预期 2 行" in proc.stderr
    assert "spd" in proc.stderr, f"报错没点名 spd 链，运维看不出少了什么：{proc.stderr}"


# ---------------------------------------------------------------- 5. PG 拒绝


@requires_shell_tools
def test_演练脚本遇PG备份包直接拒绝而不是假装成功(tmp_path):
    """`restore_drill.sh:33-36` 对 PG 包 exit 2。钉住它——"演练通过"必须名副其实。

    这里手工拼一个 PG 形态的包（`database.dump` 而非 `database.sqlite`），因为
    `backup.sh` 的 PG 分支要真的 `pg_dump`，本环境没有；包的形状按 backup.sh 的
    postgresql 分支如实照搬。

    防假绿：断言 stdout 里已经打出了清单（`db_url_scheme=postgresql`），说明脚本
    确实走完了校验+解包才在 PG 分支上停下——否则 exit 2 可能只是包坏了的巧合。
    """
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "database.dump").write_bytes(b"PGDMP\x00fake-custom-format-dump")
    (stage / "secret.fingerprint").write_text("unset\n", encoding="utf-8")
    (stage / "manifest.txt").write_text(
        "backup_at_utc=20260101T000000Z\n"
        "db_url_scheme=postgresql\n"
        "upload_dir=uploads\n"
        "upload_dir_basename=uploads\n"
        "alembic_head_count=2\n",
        encoding="utf-8",
    )
    archive = tmp_path / "medplat-backup-pg.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=".")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    _sidecar(archive).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

    proc = _run_script("restore_drill.sh", str(archive))
    assert proc.returncode == 2, f"PG 包没被拒绝：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "db_url_scheme=postgresql" in proc.stdout, "还没解包就退出了，exit 2 不一定是 PG 分支"
    assert "只演练 SQLite" in proc.stderr
    assert "独立演练库" in proc.stderr, "拒绝了却没告诉运维该怎么办"


# ---------------------------------------------------------------- 6. 两条护栏


@requires_shell_tools
def test_备份不会给不存在的库备出一个空包(tmp_path):
    """`backup.sh:31-35` 的护栏：库文件不存在就退出，绝不让 sqlite3 顺手新建一个空库。

    这条护栏本身就是防假绿的：连接串写错（少一个斜杠、相对路径跑错 cwd）时若不拦，
    备份会**照样 exit 0**，包里是一个 0 表的空库——"备份一直在跑、里面什么都没有"，
    出事那天才发现。所以这条必须有用例守着。
    """
    out_dir = tmp_path / "backups"
    env = _script_env(
        db_path=tmp_path / "从来不存在.db", upload_dir=tmp_path / "uploads", secret="s"
    )
    proc = _run_script("backup.sh", str(out_dir), env=env, cwd=tmp_path)

    assert proc.returncode == 2, f"库不存在却备份成功了：rc={proc.returncode}\n{proc.stdout}"
    assert "SQLite 库文件不存在" in proc.stderr
    assert "不会替你新建空库" in proc.stderr
    assert list(out_dir.glob("*.tar.gz")) == [], "不该产出任何备份包"


@requires_shell_tools
def test_密钥指纹不符时恢复停下来问而不是静默恢复(tmp_path, dual_head_backup, alembic_heads):
    """`restore.sh:38-48`：密钥变了就停（exit 3），要继续得显式 `--force`。

    为什么值得单独一条：密钥不打进备份包（打进去等于锁和钥匙同盒），只记指纹。
    指纹不符时若静默恢复，审计哈希链会全部验不过，而那时人们通常会误判成"审计被篡改了"
    ——排查方向从一开始就是错的。

    防假绿：断言库确实**没被**恢复（还是自己的标记行），否则"停下来了"只是句口号。
    """
    archive, _secret = dual_head_backup
    live_db = tmp_path / "live.db"
    live_upload = tmp_path / "uploads"
    live_upload.mkdir()
    _build_synthetic_db(live_db, heads=alembic_heads, marker="换过密钥的现场")
    env = _script_env(db_path=live_db, upload_dir=live_upload, secret="换过的另一把密钥")

    proc = _run_script("restore.sh", str(archive), env=env, cwd=tmp_path)
    assert proc.returncode == 3, f"密钥不符却照样恢复：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "审计哈希链" in proc.stderr
    assert "--force" in proc.stderr
    assert _usernames(live_db) == ["换过密钥的现场"], "已经把库覆盖掉了，'停下来问'是假的"
