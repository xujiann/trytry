#!/usr/bin/env node
/*
 * 下钻明细（`openDrilldown`）的等价性取证（ADR-0009 第十一批）。
 *
 * 为什么单独写一个：`scripts/render_diff.js` 只跑**渲染路径**——页面渲染完就收工。
 * 而 `openDrilldown` 要**点击**指标卡或预警横幅才走到，比对器一个字节都覆盖不到。
 * 第十一批把这个函数里遮住 `panel()` 组件的同名局部变量 `panel` 改成了 `drill`，
 * 是个纯局部改名、理应字节不变——但"理应"不是证据，所以在这里真跑一遍。
 *
 * 做法与 render_diff.js 同源：拿 `--base`（缺省 HEAD）那一版的 static/ 与工作区版
 * 各跑一次同一个 `openDrilldown`，比 `#drill-panel` 的 innerHTML 与 dataset。
 * `offset` 取 0 / 20 / 40，覆盖"无上一页 / 两页都有 / 无下一页"三条分页分支。
 *
 * 与 render_diff.js 一样：**不进 CI、不进 Makefile、不加任何 npm 依赖**（只用
 * 内置 fs/vm/path/child_process），删掉它对应用没有任何影响。
 *
 * 用法：
 *   node scripts/drilldown_equiv.js [--base <git-ref>]
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const STATIC_REL = "server/app/static";
const FILES = ["shared.js", "core.js"];

/* 明细夹具：文本字段一律塞载荷（与 render_fixtures.json 同一条约定），
 * 这样比的就不只是"标签没挪位"，还有"转义行为没变"。
 * `item: null` 那条走 `row[f] ?? "—"` 的兜底支。 */
const DRILLDOWN = {
  label: "危急值 & 未处理",
  total: 42,
  page: "共享中心检查<b>",
  columns: ["ID", "患者", "项目"],
  fields: ["id", "patient", "item"],
  items: [
    { id: 1, patient: "张三 &", item: "钾 6.8<b>" },
    { id: 2, patient: "李四", item: null },
  ],
};

async function run(staticDir, offset) {
  // 只需要一个能记住 innerHTML 与 dataset 的替身元素——`openDrilldown` 对 DOM 的
  // 全部要求就这些（外加 classList.remove）。
  const el = { innerHTML: "", dataset: {}, classList: { remove() {}, add() {} } };
  const ctx = vm.createContext({
    document: { addEventListener() {}, querySelector: () => el },
    window: { addEventListener() {} },
    localStorage: { getItem: () => null, setItem() {} },
    encodeURIComponent, JSON, console,
  });
  for (const f of FILES) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, f), "utf8"), ctx, { filename: f });
  }
  ctx.$ = () => el;                                   // 页面文件加载完再盖，同 render_diff.js
  ctx.api = async () => JSON.parse(JSON.stringify(DRILLDOWN));
  await ctx.openDrilldown("critical_values", offset);
  return JSON.stringify({ html: el.innerHTML, dataset: el.dataset });
}

function exportBase(ref) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "drilldown-"));
  for (const f of FILES) {
    const buf = execFileSync("git", ["show", `${ref}:${STATIC_REL}/${f}`], { cwd: ROOT, maxBuffer: 64 * 1024 * 1024 });
    fs.writeFileSync(path.join(dir, f), buf);
  }
  return dir;
}

async function main() {
  const argv = process.argv.slice(2);
  const base = argv[0] === "--base" ? argv[1] : "HEAD";
  const baseDir = exportBase(base);
  let bad = 0;
  try {
    for (const offset of [0, 20, 40]) {
      const [before, after] = [await run(baseDir, offset), await run(path.join(ROOT, STATIC_REL), offset)];
      if (before === after) {
        console.log(`✓ offset=${offset}：逐字符相同（${before.length} 字符）`);
      } else {
        console.log(`✗ offset=${offset}：输出有差异\n  前: ${before}\n  后: ${after}`);
        bad++;
      }
    }
  } finally {
    fs.rmSync(baseDir, { recursive: true, force: true });
  }
  process.exitCode = bad ? 1 : 0;
}

main().catch((e) => { console.error(e); process.exit(2); });
