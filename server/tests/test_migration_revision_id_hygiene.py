"""P2-2：revision id 是手写的伪 hex，彼此只差一个字符——改错一位不会报错。

40 个手写 id 里有 10 对**编辑距离为 1**（等长、恰好一位不同）。这不是审美问题：
`down_revision` 写错一位时，它落到的是**另一条真实存在的迁移**，于是

- alembic 不会报"找不到该 revision"（那个 id 真的在），
- 迁移图仍然连通，`make build` 的双 head 校验也照样过，
- 链条却被悄悄改了顺序——而生产库走的是链条，不是"从空库跑一遍"。

`test_migration_model_parity.py` 那道闸门是**从空库 upgrade heads 再比模型**，
它能发现"最终结构不对"，但发现不了"最终结构对、可升级路径顺序不对"这一种。

**存量的 10 对改不掉**：revision id 已经写进每个已部署库的 `alembic_version`，
改名等于让所有在跑的库找不到自己的当前版本。所以这里只做棘轮——
**不许再新增近似对**，把危险面钉在今天这个数上。

两条：覆盖面自证（每个文件都真解析到了、没有重复 id），近似对只减不增。
"""
import itertools
import pathlib
import re
import warnings

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"

#: 存量的 10 对近似 id（2026-09-07 实测）。**只许变少，不许变多。**
#: 改不掉的原因见模块 docstring；列在这里是为了让棘轮可审——数字对不上时
#: 能一眼看出多出来的是哪一对，而不是只知道"从 10 变成了 11"。
KNOWN_NEAR_PAIRS = {
    ("a1b2c3d4e5f6", "a1b2c3d4e5f7"),
    ("a2b3c4d5e6f7", "d2b3c4d5e6f7"),
    ("c1d2e3f4a5b2", "c1d2e3f4a5b6"),
    ("c2d3e4f5a6b7", "c9d3e4f5a6b7"),
    ("c9d0e1f2a3b4", "c9d0e1f2a3b5"),
    ("c9d3e4f5a6b7", "c9d3e4f5a6b8"),
    ("d4e5f6a7b8c1", "d4e5f6a7b8c9"),
    ("d5e6f7a8b9c0", "d5e6f7a8b9c1"),
    ("e1f2a3b4c5d6", "e1f2a3b4c5d7"),
    ("f1a2b3c4d5e6", "f8a2b3c4d5e6"),
}

#: 两种声明写法都要认：老模板 `revision = "..."`、新模板 `revision: str = "..."`。
#: 这条正则本身踩过坑——只认老写法时 94 个文件里只解析到 40 个，
#: 于是近似对数报成 3（真值 10）。**判据比它想描述的那件事窄，就会给出一个
#: 看着很确定的错答案**，所以下面第一条用例专门数"解析到几个"。
_REVISION = re.compile(r'^revision(?:\s*:\s*str)?\s*=\s*["\']([^"\']+)', re.M)


def _revisions() -> dict[str, str]:
    """revision id → 文件名。"""
    out: dict[str, str] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        m = _REVISION.search(path.read_text(encoding="utf-8"))
        assert m, f"{path.name} 里没解析到 revision——两种声明写法之外又出了第三种？"
        out[m.group(1)] = path.name
    return out


def _near(a: str, b: str) -> bool:
    """编辑距离恰为 1（等长、只有一位不同）。手写 id 全是 12 位，够用。"""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def test_覆盖面自证():
    """先证"我数了全部"，再谈数出来的结论。

    这条不是形式主义：本条目原先记的是"3 组近似"，那个 3 就是拿只认老写法的
    正则数出来的——94 个文件只看见 40 个。数字要写进账本给人做判断之前，
    先问一句"这个判据认得全吗"。
    """
    files = sorted(VERSIONS.glob("*.py"))
    revs = _revisions()
    warnings.warn(
        f"\n  [迁移 revision id 卫生] 覆盖面自证\n"
        f"    版本目录文件数：{len(files)}\n"
        f"    解析到 revision：{len(revs)}（无重复）\n"
        f"    编辑距离=1 的近似对：{len(KNOWN_NEAR_PAIRS)}（存量，改不掉，只减不增）",
        UserWarning,
        stacklevel=1,
    )
    assert len(revs) == len(files), (
        f"{len(files)} 个迁移文件只解析到 {len(revs)} 个 revision id——"
        "要么有重复 id（alembic 会以别的方式炸），要么正则没认全声明写法。"
        "两种都会让下面那条棘轮数出一个偏小的数。"
    )


def test_近似revision_id只减不增():
    """新迁移的 id 不许跟已有的只差一位。"""
    revs = _revisions()
    found = {
        tuple(sorted((a, b)))
        for a, b in itertools.combinations(sorted(revs), 2)
        if _near(a, b)
    }
    added = found - KNOWN_NEAR_PAIRS
    assert not added, (
        "新增了「只差一位」的 revision id。`down_revision` 写错一位时会落到**另一条真实"
        "存在的迁移**上：alembic 不报错、迁移图仍连通、双 head 校验也过，链条却被悄悄"
        "改了顺序。换一个明显不同的 id（别在已有 id 上改一位）：\n  "
        + "\n  ".join(f"{a} ({revs[a]})  ~  {b} ({revs[b]})" for a, b in sorted(added))
    )
    stale = KNOWN_NEAR_PAIRS - found
    assert not stale, (
        f"KNOWN_NEAR_PAIRS 里这些对已经不存在了，把它们从清单里删掉（棘轮只减不增，"
        f"删是好事，但清单要跟着缩）：{sorted(stale)}"
    )
