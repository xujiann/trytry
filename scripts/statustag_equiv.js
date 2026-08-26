#!/usr/bin/env node
/*
 * `statusTag()` 与它要替换掉的三种手写形态的**等价性证明**（ADR-0009 / P2-26）。
 *
 * 为什么需要它：收敛 33 个调用点是机械替换，但"机械"不等于"等价"。逐页拿
 * `render_diff.js` 比字节只能覆盖有夹具的那几页，而调用点散在 6 个文件里。
 * 所以换一种取证：把**表达式本身**在一个输入矩阵上跑一遍，两种写法逐字符比。
 * 表达式等价 + 替换是纯文本替换 ⇒ 每个调用点都等价，不必给每一页都造夹具。
 *
 * 矩阵刻意包含这几类 key：命中映射、未命中、`""`（`critical_status` 的列默认值
 * 就是空串，不是假想）、null / undefined、以及带 XSS 载荷的未知状态码。
 *
 * 用法：node scripts/statustag_equiv.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SHARED = path.resolve(__dirname, "..", "server/app/static/shared.js");
const ctx = vm.createContext({ document: { cookie: "" }, console });
vm.runInContext(fs.readFileSync(SHARED, "utf8"), ctx, { filename: "shared.js" });
const { statusTag, esc } = ctx;

/** 管理端 33 处的手写形态。 */
function handwrittenAdmin(map, key) {
  const [text, color] = map[key] || [key, ""];
  return `<span class="tag ${color}">${esc(text)}</span>`;
}

/** `pages-spd.js` 的 `spdTag` 与 `m/m.js` 的 `spdTagOf`（两者逐字相同）。 */
function handwrittenSpd(map, key) {
  const [text, cls] = map[key] || [key || "—", ""];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

const MAPS = {
  "常规映射": { registered: ["已登记", "orange"], delivered: ["已配送", "green"], plain: ["无配色", ""] },
  "空串也是个键": { "": ["未标记", "orange"], done: ["已完成", "green"] },
};

const KEYS = [
  ["命中且有配色", "registered"],
  ["命中但无配色", "plain"],
  ["未命中", "unknown_status"],
  ["空串（critical_status 的列默认值）", ""],
  ["null", null],
  ["undefined", undefined],
  ["未命中且带 XSS 载荷", '<img src=x onerror=alert(1)>'],
  ["未命中且带引号", `it's "a" <tag>`],
  ["数字状态码", 0],
];

let bad = 0, ok = 0, divergedByDesign = 0;
for (const [mapName, map] of Object.entries(MAPS)) {
  for (const [label, key] of KEYS) {
    // 慢专病侧只在"映射表不用假值当键"的前提下等价（前提由下面的扫描来证）。
    // `空串也是个键` 这张表就是**故意**违反前提的反例：留着它，是为了让这条前提
    // 有一个看得见的反证——如果哪天扫描失效了，这里的分叉说明前提是真会咬人的。
    const spdPreconditionHolds = !Object.keys(map).some((k) => !k);
    const pairs = [
      ["管理端", handwrittenAdmin(map, key), statusTag(map, key), true],
      ["慢专病", handwrittenSpd(map, key), statusTag(map, key || "—"), spdPreconditionHolds],
    ];
    for (const [side, before, after, counts] of pairs) {
      if (before === after) { if (counts) ok++; continue; }
      if (!counts) { divergedByDesign++; continue; }
      console.log(`✗ [${mapName}] ${side} · ${label}\n    手写: ${before}\n    组件: ${after}`);
      bad++;
    }
  }
}

// 慢专病侧的写法 `statusTag(map, key || "—")` 与原 `map[key] || [key || "—", ""]`
// 只在一个前提下等价：**没有任何一张 spd 映射表用假值当键**（否则 key 为假值时
// 原写法查的是 map[""]/map[0]，新写法查的是 map["—"]）。这个前提要真的查，
// 不能默认——下面把仓库里所有传给 spdTag/spdTagOf 的映射表都验一遍。
const SPD_FILES = ["server/app/static/pages-spd.js", "server/app/static/m/m.js"];
for (const rel of SPD_FILES) {
  const src = fs.readFileSync(path.resolve(__dirname, "..", rel), "utf8");
  const names = new Set([...src.matchAll(/spdTag(?:Of)?\(\s*([A-Z_][\w]*)\s*,/g)].map((m) => m[1]));
  for (const name of names) {
    const decl = new RegExp(`const ${name} = \\{([^;]*?)\\};`, "s").exec(src);
    if (!decl) { console.log(`? 找不到映射表 ${name} 的定义（${rel}），前提未验证`); bad++; continue; }
    for (const k of [...decl[1].matchAll(/(?:^|[{,])\s*("?)([\w\u4e00-\u9fa5-]*)\1\s*:/g)]) {
      if (k[2] === "" || k[2] === "0" || k[2] === "—") {
        console.log(`✗ ${rel} 的 ${name} 用了假值/破折号当键（${JSON.stringify(k[2])}），前提不成立`);
        bad++;
      }
    }
  }
  console.log(`  前提已验：${rel} 的 ${names.size} 张映射表`);
}

console.log(
  bad
    ? `\n✗ ${bad} 组不等价`
    : `✓ ${ok} 组输入下两种写法逐字符相同`
      + `（另有 ${divergedByDesign} 组是**故意**违反前提的反例，已由上面的前提扫描排除）`,
);
process.exitCode = bad ? 1 : 0;
