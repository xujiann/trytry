/**
 * 县域医共体信息化平台（medplat）· 客户与投资方介绍 PPT 生成脚本
 *
 * 用法：node build_deck.js [输出路径]
 * 数字口径：全部取自仓库代码实际状态（OpenAPI 路由表 / ORM 元数据 / 页面注册表 /
 * pytest 收集数），以及仓库内政策依据文档，不写未经验证的市场或业绩数据。
 */
const pptxgen = require("pptxgenjs");
const path = require("path");

const OUT = process.argv[2] || path.join(__dirname, "县域医共体信息化平台介绍.pptx");

/* ── 设计系统 ───────────────────────────────────────────────── */
const C = {
  ink: "0E3B36", // 深墨绿（暗场主色）
  ink2: "082925", // 更深，用于暗场上的卡片
  teal: "18867A", // 品牌主色
  tealD: "116C62",
  tealL: "5FB3A6",
  mint: "9BD8CD",
  amber: "E39B33", // 锐利强调色
  light: "F4F8F7", // 亮场背景
  tint: "E6F1EF", // 亮场卡片底
  white: "FFFFFF",
  text: "16302C", // 亮场正文
  muted: "62807A", // 亮场辅助文字
  mutedD: "9FBDB6", // 暗场辅助文字
};
const F = { cn: "微软雅黑", num: "Arial" };
const W = 13.333;
const M = 0.7; // 页边距
const CW = W - M * 2; // 内容宽度 11.933

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "medplat";
pres.title = "县域医共体信息化平台介绍";

/* ── 工具函数（每次返回新对象，避免 pptxgenjs 就地改写选项） ──── */
const shadow = (o = {}) => ({
  type: "outer",
  angle: 90,
  blur: 12,
  offset: 2,
  color: "0E3B36",
  opacity: 0.1,
  ...o,
});

function newSlide(dark) {
  const s = pres.addSlide();
  s.background = { color: dark ? C.ink : C.light };
  return s;
}

/** 标题区：kicker（小字提示）+ 主标题 + 可选副标题，无下划线、无色条 */
function head(s, { kicker, title, sub, dark = false, y = 0.52 }) {
  let cy = y;
  if (kicker) {
    s.addText(kicker, {
      x: M, y: cy, w: CW, h: 0.28,
      fontFace: F.cn, fontSize: 12, bold: true, charSpacing: 2,
      color: dark ? C.mint : C.teal, margin: 0, valign: "middle",
    });
    cy += 0.36;
  }
  s.addText(title, {
    x: M, y: cy, w: CW, h: 0.62,
    fontFace: F.cn, fontSize: 32, bold: true,
    color: dark ? C.white : C.text, margin: 0, valign: "middle",
  });
  cy += 0.7;
  if (sub) {
    s.addText(sub, {
      x: M, y: cy, w: CW, h: 0.44,
      fontFace: F.cn, fontSize: 13,
      color: dark ? C.mutedD : C.muted, margin: 0, valign: "top",
    });
    cy += 0.5;
  }
  return cy + 0.16;
}

/** 圆角卡片 */
function card(s, x, y, w, h, opt = {}) {
  s.addShape(pres.ShapeType.roundRect, {
    x, y, w, h,
    rectRadius: opt.radius ?? 0.1,
    fill: { color: opt.fill || C.white },
    line: opt.line ? { color: opt.line, width: 1 } : { type: "none" },
    shadow: opt.shadow === false ? undefined : shadow(opt.shadowOpt || {}),
  });
}

/** 圆形徽章（重复的视觉母题） */
function badge(s, text, x, y, d = 0.44, fill = C.teal, color = C.white, size = 13) {
  s.addShape(pres.ShapeType.ellipse, {
    x, y, w: d, h: d, fill: { color: fill }, line: { type: "none" },
  });
  s.addText(text, {
    x, y, w: d, h: d,
    fontFace: F.cn, fontSize: size, bold: true, color,
    align: "center", valign: "middle", margin: 0,
  });
}

/** 大数字统计块 */
function stat(s, x, y, w, num, unit, label, { dark = false, numColor, size = 40 } = {}) {
  const runs = [{ text: num, options: { fontFace: F.num, fontSize: size, bold: true, color: numColor || (dark ? C.mint : C.teal) } }];
  if (unit) runs.push({ text: unit, options: { fontFace: F.cn, fontSize: 13, bold: true, color: dark ? C.mutedD : C.muted } });
  s.addText(runs, { x, y, w, h: 0.66, margin: 0, valign: "bottom" });
  s.addText(label, {
    x, y: y + 0.68, w, h: 0.3,
    fontFace: F.cn, fontSize: 11.5, color: dark ? C.mutedD : C.muted, margin: 0, valign: "top",
  });
}

/** 步骤之间的箭头 */
function arrow(s, x, y, w, h, dark = false) {
  s.addText("→", {
    x, y, w, h,
    fontFace: F.cn, fontSize: 16, bold: true,
    color: dark ? C.tealL : C.tealL,
    align: "center", valign: "middle", margin: 0,
  });
}

function bullets(s, items, x, y, w, h, { size = 11.5, color = C.text, gap = 6 } = {}) {
  s.addText(
    items.map((t, i) => ({
      text: t,
      options: { bullet: { code: "2022" }, breakLine: i !== items.length - 1 },
    })),
    {
      x, y, w, h,
      fontFace: F.cn, fontSize: size, color,
      lineSpacingMultiple: 1.18, paraSpaceAfter: gap, margin: 0, valign: "top",
    }
  );
}

function pageNote(s, text, dark = false) {
  s.addText(text, {
    x: M, y: 6.92, w: CW, h: 0.3,
    fontFace: F.cn, fontSize: 9.5,
    color: dark ? C.mutedD : C.muted, margin: 0, valign: "middle",
  });
}

/* ══════════════════════════════════════════════════════════════
   1 · 封面
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  // 母题：右上角的柔光同心圆
  s.addShape(pres.ShapeType.ellipse, {
    x: 9.1, y: -1.5, w: 5.6, h: 5.6, fill: { color: C.teal, transparency: 78 }, line: { type: "none" },
  });
  s.addShape(pres.ShapeType.ellipse, {
    x: 10.3, y: -0.3, w: 3.2, h: 3.2, fill: { color: C.tealL, transparency: 82 }, line: { type: "none" },
  });

  s.addText("紧密型县域医共体 · 信息化整体解决方案", {
    x: M, y: 1.55, w: 9.4, h: 0.34,
    fontFace: F.cn, fontSize: 13, bold: true, charSpacing: 2, color: C.mint, margin: 0, valign: "middle",
  });
  s.addText("县域医共体信息化平台", {
    x: M, y: 2.0, w: 10.4, h: 0.95,
    fontFace: F.cn, fontSize: 46, bold: true, color: C.white, margin: 0, valign: "middle",
  });
  s.addText([
    { text: "medplat", options: { fontFace: F.num, fontSize: 22, bold: true, color: C.amber } },
    { text: "   +  全域慢专病全流程管理子系统", options: { fontFace: F.cn, fontSize: 16, color: C.mint } },
  ], { x: M, y: 3.0, w: 10.4, h: 0.42, margin: 0, valign: "middle" });

  s.addText("一个平台贯通县—乡—村三级，横向联动医疗 · 医药 · 医保 · 公卫", {
    x: M, y: 3.62, w: 10.4, h: 0.36,
    fontFace: F.cn, fontSize: 15, color: C.white, margin: 0, valign: "middle",
  });

  const st = [
    ["36", "项", "《功能指引》功能全覆盖"],
    ["946", "个", "后端业务接口"],
    ["259", "张", "统一数据模型表"],
    ["2,360", "项", "自动化测试守护"],
  ];
  const sw = 2.75, sg = 0.34;
  st.forEach((v, i) => {
    const x = M + i * (sw + sg);
    stat(s, x, 4.72, sw, v[0], v[1], v[2], { dark: true, numColor: C.white, size: 34 });
  });

  s.addText("客户与投资方介绍材料 · 2026", {
    x: M, y: 6.75, w: CW, h: 0.3,
    fontFace: F.cn, fontSize: 11, color: C.mutedD, margin: 0, valign: "middle",
  });
  s.addNotes("开场：本平台依据国卫办规划函〔2025〕63号《紧密型县域医共体信息化功能指引》建设，五大类36项功能已全部落地；封面四个数字来自代码实际状态，可现场复核。");
}

/* ══════════════════════════════════════════════════════════════
   2 · 目录
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  head(s, { kicker: "CONTENTS", title: "目录" });
  const items = [
    ["01", "政策机遇与行业痛点", "确定性需求从哪来，钱为什么会花在这里"],
    ["02", "平台总览与功能全景", "五大类 36 项功能、三端载体、平台规模"],
    ["03", "四大核心业务场景", "共享中心 · 药事协同 · 医防融合 · 运营监管"],
    ["04", "拳头产品：全域慢专病", "十一端一体，163 条需求逐条对照"],
    ["05", "技术架构与工程质量", "架构、治理体系、安全合规与信创"],
    ["06", "交付、价值与投资亮点", "怎么落地，客户得到什么，我们的护城河"],
  ];
  const cw = (CW - 0.34) / 2, ch = 1.24, gx = 0.34, gy = 0.3;
  items.forEach((it, i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = M + col * (cw + gx), y = 2.16 + row * (ch + gy);
    card(s, x, y, cw, ch, { fill: C.white });
    s.addText(it[0], {
      x: x + 0.3, y: y + 0.2, w: 0.9, h: 0.5,
      fontFace: F.num, fontSize: 30, bold: true, color: C.mint, margin: 0, valign: "middle",
    });
    s.addText(it[1], {
      x: x + 1.24, y: y + 0.24, w: cw - 1.5, h: 0.42,
      fontFace: F.cn, fontSize: 17, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(it[2], {
      x: x + 1.24, y: y + 0.68, w: cw - 1.5, h: 0.4,
      fontFace: F.cn, fontSize: 11.5, color: C.muted, margin: 0, valign: "top",
    });
  });
  s.addNotes("六个部分：前两部分讲市场与产品，中间两部分讲业务，后两部分讲工程与商业。");
}

/* ══════════════════════════════════════════════════════════════
   3 · 政策机遇
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "01 · 市场机遇",
    title: "政策驱动的确定性需求",
    sub: "国家已形成「指导意见—重点工作—参考手册—功能指引—监测指标」完整体系，信息化是医共体统一管理与业务协同的基础支撑",
  });

  // 左：四份政策依据
  const lw = 5.9;
  s.addText("平台建设的四份直接依据", {
    x: M, y: y0, w: lw, h: 0.32,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.text, margin: 0, valign: "middle",
  });
  const docs = [
    ["1", "《信息化功能指引》", "国卫办规划函〔2025〕63号 · 五大类 36 项功能，功能设计直接依据"],
    ["2", "2025 年重点工作通知", "国卫办基层函〔2025〕121号 · 明确年度约束性目标"],
    ["3", "《建设参考手册（第一版）》", "国卫基层运行便函〔2025〕4号 · 组建、运行与医防融合框架"],
    ["4", "《监测指标体系（2024版）》", "5 个一级指标、14 个二级监测指标，成效评价的标尺"],
  ];
  docs.forEach((d, i) => {
    const y = y0 + 0.44 + i * 0.98;
    card(s, M, y, lw, 0.86, { fill: C.white });
    badge(s, d[0], M + 0.24, y + 0.21, 0.44, C.teal, C.white, 13);
    s.addText(d[1], {
      x: M + 0.84, y: y + 0.12, w: lw - 1.1, h: 0.32,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(d[2], {
      x: M + 0.84, y: y + 0.44, w: lw - 1.06, h: 0.34,
      fontFace: F.cn, fontSize: 10, color: C.muted, margin: 0, valign: "top",
    });
  });

  // 右：2025 约束性目标
  const rx = M + lw + 0.34, rw = CW - lw - 0.34;
  s.addText("2025 年底前的约束性目标（121号文）", {
    x: rx, y: y0, w: rw, h: 0.32,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.text, margin: 0, valign: "middle",
  });
  const targets = [
    ["90%", "以上", "县建成紧密型县域医共体"],
    ["80%", "以上", "乡镇卫生院被县域影像中心覆盖"],
    ["50%", "以上", "县域中心药房建设率"],
    ["70%", "以上", "电子健康档案向本人开放"],
  ];
  const tw = (rw - 0.28) / 2, th = 1.28;
  targets.forEach((t, i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = rx + col * (tw + 0.28), y = y0 + 0.44 + row * (th + 0.28);
    card(s, x, y, tw, th, { fill: C.ink });
    s.addText([
      { text: t[0], options: { fontFace: F.num, fontSize: 30, bold: true, color: C.amber } },
      { text: " " + t[1], options: { fontFace: F.cn, fontSize: 12, color: C.mint } },
    ], { x: x + 0.24, y: y + 0.2, w: tw - 0.44, h: 0.48, margin: 0, valign: "middle" });
    s.addText(t[2], {
      x: x + 0.24, y: y + 0.72, w: tw - 0.44, h: 0.44,
      fontFace: F.cn, fontSize: 11, color: C.white, margin: 0, valign: "top",
    });
  });
  s.addText("另有硬性要求：县级与市级医疗机构检查检验结果互认项目超 200 项，医共体内常规检查检验项目基本实现互认，全面推行「先诊疗、后结算」。", {
    x: rx, y: y0 + 0.44 + 2 * (th + 0.28) + 0.06, w: rw, h: 0.7,
    fontFace: F.cn, fontSize: 10.5, color: C.muted, margin: 0, valign: "top",
  });
  pageNote(s, "来源：国卫办基层函〔2025〕121号、国卫办规划函〔2025〕63号、国卫基层运行便函〔2025〕4号、《紧密型县域医疗卫生共同体监测指标体系（2024版）》。");
  s.addNotes("这一页回答投资方最关心的问题：需求是政策强制的，不是可选项；且有明确的时间表和验收标尺。");
}

/* ══════════════════════════════════════════════════════════════
   4 · 行业痛点
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "01 · 需求诊断",
    title: "县域信息化的五个真问题",
    sub: "来自三明、东台、嘉善等地的需求诊断，也是每一份县域招标文件反复出现的条目",
  });
  const pains = [
    ["标准脱节", "县乡村分散建设，诊断 / 药品 / 耗材 / 收费四类编码不统一，与国家数据标准体系脱节——这是结果互认与业务联动做不成的根因。"],
    ["医卫割裂", "医疗与公共卫生两套系统各管一段，公卫数据存在「漏、空、假」，医防融合缺少信息化载体。"],
    ["协同不畅", "医共体内检查检验不能互认，双向转诊缺乏系统支撑，基层首诊率低，优质资源沉不下去。"],
    ["管理粗放", "人财物分散管理，缺乏覆盖事前预警、事中控制、事后评价的运营监管与绩效考核工具。"],
    ["体验割裂", "院前、院中、院后流程未打通，「一老一小」等重点人群看病难、看病烦问题突出。"],
  ];
  const rh = 0.82, rg = 0.16;
  pains.forEach((p, i) => {
    const y = y0 + 0.12 + i * (rh + rg);
    card(s, M, y, CW, rh, { fill: C.white });
    badge(s, String(i + 1), M + 0.28, y + 0.19, 0.44, i < 2 ? C.amber : C.teal, C.white, 13);
    s.addText(p[0], {
      x: M + 0.9, y: y + 0.16, w: 1.6, h: 0.5,
      fontFace: F.cn, fontSize: 15, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(p[1], {
      x: M + 2.5, y: y + 0.16, w: CW - 2.9, h: 0.5,
      fontFace: F.cn, fontSize: 11.5, color: C.muted, margin: 0, valign: "middle",
    });
  });
  s.addNotes("五个痛点对应后面的解决方案：统一编码与主索引解决第1条，医防融合模块解决第2条，共享中心与转诊解决第3条，驾驶舱与绩效解决第4条，三端载体解决第5条。");
}

/* ══════════════════════════════════════════════════════════════
   5 · 解决方案总览（分层架构）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "02 · 总体方案",
    title: "一个平台、一套数据中心、N 类协同应用",
    sub: "对标功能指引「建设模式」四项要求：省市统筹集约建设 · 统一信创网络 · 优化协同共享 · 完善安全保障",
  });
  const layers = [
    ["服务门户层", "居民端 H5（6 页签）· 医生移动端 H5（8 页签）· 管理端 SPA（89 页面），一数多屏", "E3F0EE", C.text],
    ["协同应用层", "五大类 36 项功能 + 全域慢专病全流程管理子系统（十一端一体）", "C9E5E0", C.text],
    ["平台支撑层", "统一认证与患者主索引 EMPI · 规则引擎 · 流程引擎 · 领域事件总线 · 消息与定时调度", "A6D6CD", C.text],
    ["数据中心层", "259 张表统一数据模型 · 诊断/药品/耗材/收费「四统一」编码 · 数据质控与治理", "6FBCAF", C.text],
    ["集成对接层", "HL7 v2 ADT / FHIR R4 入站与导出 · ESB 接入注册与消息队列 · 多源数据采集器", "2A9184", C.white],
    ["安全运维层", "RBAC + 横向数据隔离 · 审计哈希链 · PII 列加密 · 备份容灾与恢复演练", "116C62", C.white],
  ];
  const lh = 0.62, lg = 0.12;
  layers.forEach((l, i) => {
    const y = y0 + 0.14 + i * (lh + lg);
    card(s, M, y, CW, lh, { fill: l[2], shadow: false });
    s.addText(l[0], {
      x: M + 0.32, y, w: 2.0, h: lh,
      fontFace: F.cn, fontSize: 14, bold: true, color: l[3], margin: 0, valign: "middle",
    });
    s.addText(l[1], {
      x: M + 2.3, y, w: CW - 2.62, h: lh,
      fontFace: F.cn, fontSize: 11.5, color: l[3], margin: 0, valign: "middle",
    });
  });
  pageNote(s, "部署形态：政务云 / 卫生健康专有云集约化部署；县域多个医共体按「主中心 + 分中心」组网，不重复建设县级数据中心机房。");
  s.addNotes("自上而下讲：用户看到的三端 → 业务功能 → 平台能力 → 数据 → 对接 → 安全。强调数据中心层是一套统一模型，不是把各家系统拼起来。");
}

/* ══════════════════════════════════════════════════════════════
   6 · 五大类 36 项功能全景
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "02 · 功能全景",
    title: "五大类 36 项功能，逐项对照全部落地",
    sub: "每一项都在平台内可运行、有自动化测试；主项与子功能两级逐条对照表可直接用于招标响应与验收",
  });
  const cats = [
    ["7", "区域医疗服务协同", ["影像 · 心电 · 检验 · 病理", "远程会诊中心", "消毒供应中心", "县域智慧急救中心"], C.teal],
    ["9", "便民惠民服务协同", ["电子健康卡与就诊凭据", "互联网 + 诊疗 / 慢病 / 家医", "预约诊疗与双向转诊", "中医智能辅诊与共享中药房", "基层缺药登记与用药监测"], C.tealD],
    ["5", "医疗管理服务协同", ["检查检验结果互认", "县域集中审方中心", "医保业务协同", "远程医学教育", "中医药适宜技术推广"], C.teal],
    ["7", "公共卫生服务协同", ["慢病 · 老年 · 妇幼 · 疫苗", "突发公卫事件应急指挥", "多点触发监测预警", "基层医防协同提醒"], C.tealD],
    ["8", "基层医疗卫生综合管理", ["综合决策可视化驾驶舱", "人力 / 财务 / 物资 / 药耗统管", "行政 OA 一体化", "医共体绩效统一考核", "医疗废弃物全过程追溯"], C.teal],
  ];
  const cw = 2.25, cg = 0.17, ch = 3.9;
  cats.forEach((c, i) => {
    const x = M + i * (cw + cg), y = y0 + 0.18;
    card(s, x, y, cw, ch, { fill: C.white });
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.22, y: y + 0.24, w: 0.62, h: 0.62, rectRadius: 0.08,
      fill: { color: c[3] }, line: { type: "none" },
    });
    s.addText(c[0], {
      x: x + 0.22, y: y + 0.24, w: 0.62, h: 0.62,
      fontFace: F.num, fontSize: 22, bold: true, color: C.white,
      align: "center", valign: "middle", margin: 0,
    });
    s.addText("项", {
      x: x + 0.88, y: y + 0.24, w: 0.5, h: 0.62,
      fontFace: F.cn, fontSize: 11, color: C.muted, valign: "middle", margin: 0,
    });
    s.addText(c[1], {
      x: x + 0.22, y: y + 1.0, w: cw - 0.44, h: 0.62,
      fontFace: F.cn, fontSize: 13.5, bold: true, color: C.text, margin: 0, valign: "top",
    });
    bullets(s, c[2], x + 0.22, y + 1.7, cw - 0.4, ch - 1.86, { size: 9.5, color: C.muted, gap: 5 });
  });
  s.addText("36 项主功能 · 379 项子功能已按指引清单逐条重审", {
    x: M, y: y0 + 0.18 + ch + 0.22, w: CW, h: 0.34,
    fontFace: F.cn, fontSize: 12, bold: true, color: C.teal, margin: 0, valign: "middle",
  });
  s.addNotes("这一页是招标响应的底气：不是「支持」而是「已实现且有测试」。可现场打开对照表逐条走查。");
}

/* ══════════════════════════════════════════════════════════════
   7 · 平台规模（暗场）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  const y0 = head(s, {
    dark: true,
    kicker: "02 · 平台规模",
    title: "不是 PPT 里的功能，是代码里的功能",
    sub: "以下数字全部由代码实际状态生成——OpenAPI 路由表、ORM 元数据、前端页面注册表、测试收集数，可现场重新生成复核",
  });
  const row1 = [
    ["946", "个", "后端业务接口（730 条路径）"],
    ["90", "个", "业务域（OpenAPI 标签）"],
    ["259", "张", "统一数据模型表"],
    ["89", "个", "管理端页面（8 个导航分组）"],
  ];
  const row2 = [
    ["2,360", "项", "自动化测试用例"],
    ["6", "项", "CI 阻断门（全部强制）"],
    ["89", "个", "数据库结构迁移"],
    ["14", "份", "架构决策记录 ADR"],
  ];
  const cw = (CW - 3 * 0.3) / 4, chh = 1.62;
  [row1, row2].forEach((row, r) => {
    row.forEach((v, i) => {
      const x = M + i * (cw + 0.3), y = y0 + 0.26 + r * (chh + 0.3);
      card(s, x, y, cw, chh, { fill: C.ink2, shadow: false });
      s.addText([
        { text: v[0], options: { fontFace: F.num, fontSize: 40, bold: true, color: r === 0 ? C.white : C.amber } },
        { text: " " + v[1], options: { fontFace: F.cn, fontSize: 13, color: C.mint } },
      ], { x: x + 0.28, y: y + 0.26, w: cw - 0.5, h: 0.66, margin: 0, valign: "middle" });
      s.addText(v[2], {
        x: x + 0.28, y: y + 0.96, w: cw - 0.5, h: 0.5,
        fontFace: F.cn, fontSize: 11, color: C.mutedD, margin: 0, valign: "top",
      });
    });
  });
  pageNote(s, "代码规模：后端 Python 约 6.2 万行，测试代码约 5.2 万行（测试与实现接近 1:1），免构建前端约 0.96 万行。", true);
  s.addNotes("投资方视角：这是一个真实完成度很高的产品，不是 demo。测试代码量与实现代码量接近 1:1 是关键佐证。");
}

/* ══════════════════════════════════════════════════════════════
   8 · 场景一：七大资源共享中心
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "03 · 核心场景一",
    title: "基层检查 · 上级诊断 · 结果互认",
    sub: "落实「分布式检查、集中式诊断」，把优质诊断能力沉到乡村，把重复检查的钱省下来",
  });

  const steps = [
    ["基层开单采集", "乡镇/村室完成检查采集，电子开单与条码追溯"],
    ["上传共享中心", "影像 / 波形 / 标本随流程流转，冷链与核收留痕"],
    ["中心集中诊断", "上级医师领取、出具报告，排班与质控可管"],
    ["报告回传互认", "结果回传基层并进入互认目录，不互认须填理由"],
  ];
  const sw = 2.72, sg = 0.34, sh = 1.30;
  steps.forEach((st, i) => {
    const x = M + i * (sw + sg), y = y0 + 0.12;
    card(s, x, y, sw, sh, { fill: C.white });
    badge(s, String(i + 1), x + 0.24, y + 0.2, 0.4, C.teal, C.white, 12);
    s.addText(st[0], {
      x: x + 0.72, y: y + 0.18, w: sw - 0.92, h: 0.44,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(st[1], {
      x: x + 0.24, y: y + 0.64, w: sw - 0.46, h: 0.42,
      fontFace: F.cn, fontSize: 10, color: C.muted, margin: 0, valign: "top",
    });
    if (i < steps.length - 1) arrow(s, x + sw, y, sg, sh);
  });

  s.addText("危急值全程闭环：报告出具即秒级广播 → 接收确认 → 处置反馈，全链路留痕，端到端测试覆盖", {
    x: M, y: y0 + 1.58, w: CW, h: 0.34,
    fontFace: F.cn, fontSize: 11.5, bold: true, color: C.amber, margin: 0, valign: "middle",
  });

  const centers = [
    ["医学影像", "申请 · 质控 · 移动诊断"],
    ["心电诊断", "波形上传 · 集中判读"],
    ["医学检验", "采样 · 冷链 · 核收 · 发布"],
    ["病理诊断", "标本流转 · 冷缺血质控"],
    ["远程会诊", "受理 · 意见 · 评价 · 计费"],
    ["消毒供应", "灭菌 · 发放 · 回收追溯"],
    ["智慧急救", "调度 · 体征回传 · 上车即入院"],
  ];
  const gw = (CW - 6 * 0.14) / 7, gh = 1.55;
  centers.forEach((c, i) => {
    const x = M + i * (gw + 0.14), y = y0 + 2.2;
    card(s, x, y, gw, gh, { fill: C.tint, shadow: false });
    s.addText(c[0], {
      x: x + 0.12, y: y + 0.4, w: gw - 0.24, h: 0.46,
      fontFace: F.cn, fontSize: 12.5, bold: true, color: C.tealD,
      align: "center", valign: "middle", margin: 0,
    });
    s.addText(c[1], {
      x: x + 0.1, y: y + 0.88, w: gw - 0.2, h: 0.6,
      fontFace: F.cn, fontSize: 9, color: C.muted,
      align: "center", valign: "top", margin: 0,
    });
  });
  pageNote(s, "互认能力：开单前互认检查、互认建单、不互认理由留痕、互认量与节约测算统计——直接支撑「互认项目超 200 项」的监管口径。");
  s.addNotes("这是县域信息化的第一优先级，也是121号文年底前的硬指标。七个中心平台全部具备。");
}

/* ══════════════════════════════════════════════════════════════
   9 · 场景二：药事协同
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "03 · 核心场景二",
    title: "从「每方必审」到「村村有药」",
    sub: "支撑总药师制度与县域中心药房建设：事前监控、事中干预、事后点评，县乡村用药一盘棋",
  });
  const cols = [
    ["集中审方", C.teal, [
      "「系统 + 药师」双重审方，覆盖门诊处方、住院医嘱与互联网处方",
      "内置 50 条规则库：剂量、相互作用、禁忌诊断、特殊人群、肝肾功能提示",
      "超量拦截、审方权限限定药师角色",
      "事后处方点评与点评要点，形成闭环",
    ]],
    ["中心药房", C.tealD, [
      "库存台账、县乡村余缺调拨、缺药预警",
      "供应商、采购验收、盘点全流程",
      "药品采购建议（按消耗与库存自动测算）",
      "共享中药房：下单 → 调配 → 煎煮 → 配送 → 送达全程追溯",
    ]],
    ["用药监测", C.teal, [
      "基层缺药登记 → 临时采购 → 配送 → 取药判定，履约率可考核",
      "居民用药画像与多重用药预警",
      "全县用药地图与品种排名",
      "门诊/住院药占比、抗菌药物使用强度（DDDs/百人天）",
    ]],
  ];
  const cw = (CW - 2 * 0.34) / 3, ch = 3.0;
  cols.forEach((c, i) => {
    const x = M + i * (cw + 0.34), y = y0 + 0.16;
    card(s, x, y, cw, ch, { fill: C.white });
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.26, y: y + 0.28, w: 1.5, h: 0.42, rectRadius: 0.08,
      fill: { color: c[1] }, line: { type: "none" },
    });
    s.addText(c[0], {
      x: x + 0.26, y: y + 0.28, w: 1.5, h: 0.42,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.white,
      align: "center", valign: "middle", margin: 0,
    });
    bullets(s, c[2], x + 0.26, y + 0.92, cw - 0.5, ch - 1.1, { size: 11, color: C.text, gap: 9 });
  });
  const kpis = [["50", "条", "内置审方规则"], ["3", "级", "县乡村余缺调拨"], ["5", "段", "共享中药房全程追溯"]];
  kpis.forEach((k, i) => {
    const x = M + i * ((CW - 2 * 0.34) / 3 + 0.34);
    stat(s, x + 0.26, y0 + 0.16 + ch + 0.12, cw - 0.5, k[0], k[1], k[2], { size: 26 });
  });
  pageNote(s, "支撑口径：审方量与拦截量、缺药登记履约率、余缺调拨节约测算均可直接进考核，不需要额外统计口径。");
  s.addNotes("药事是县域最容易见效也最容易量化的模块：审方量、缺药履约率、调拨节约都能直接进考核。");
}

/* ══════════════════════════════════════════════════════════════
   10 · 场景三：医防融合
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "03 · 核心场景三",
    title: "以健康为中心：防 — 筛 — 诊 — 治 — 管",
    sub: "把公共卫生与临床装进同一套数据里，诊间即可完成建档、签约、随访与调阅，公卫数据不再「漏、空、假」",
  });
  const flow = ["防", "筛", "诊", "治", "管"];
  const flowDesc = ["健康宣教与危险因素干预", "机会性/主动筛查与复核", "智能辅诊与医防协同提醒", "标准路径与用药管理", "随访、评估、转诊与考核"];
  const fw = 2.06, fg = 0.4, fh = 1.06;
  flow.forEach((f, i) => {
    const x = M + i * (fw + fg), y = y0 + 0.14;
    card(s, x, y, fw, fh, { fill: C.ink, shadow: false });
    s.addText(f, {
      x: x + 0.16, y: y + 0.14, w: 0.5, h: 0.44,
      fontFace: F.cn, fontSize: 20, bold: true, color: C.amber, margin: 0, valign: "middle",
    });
    s.addText(flowDesc[i], {
      x: x + 0.16, y: y + 0.58, w: fw - 0.32, h: 0.4,
      fontFace: F.cn, fontSize: 9.5, color: C.mint, margin: 0, valign: "top",
    });
    if (i < flow.length - 1) arrow(s, x + fw, y, fg, fh);
  });

  const mods = [
    ["慢病一体化管理", "8 类重点病种目录（分级规则 / 指导要点 / 随访周期），目录驱动智能分级与超期预警；高血压、2 型糖尿病、高脂血症、肥胖症膳食运动指导要点已嵌入接诊与随访"],
    ["老年健康", "ADL 自理能力自动分级、失能清单、重度失能与复评到期健康预警；家庭病床与上门服务派单"],
    ["妇幼保健", "孕产妇建册 / 高危 / 访视、分娩记录、新生儿筛查与高危儿、产前筛查与诊断，出生医学证明"],
    ["传染病与监测预警", "法定传染病目录（甲类 2 小时 / 乙丙类 24 小时）、迟报统计；症候群、药品、学校缺勤等多点触发预警与应急指挥"],
    ["疫苗接种", "接种登记、禁忌拦截、接种前评估；批次冷链温控与 AEFI 上报"],
    ["随访中心", "慢病 / 出院 / 术后 / 妇幼四类随访统一任务模型，出院与手术结案自动派生任务，不靠人记"],
  ];
  const mw = (CW - 2 * 0.3) / 3, mh = 1.5;
  mods.forEach((m, i) => {
    const col = i % 3, row = Math.floor(i / 3);
    const x = M + col * (mw + 0.3), y = y0 + 1.44 + row * (mh + 0.24);
    card(s, x, y, mw, mh, { fill: C.white });
    s.addText(m[0], {
      x: x + 0.24, y: y + 0.16, w: mw - 0.48, h: 0.34,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.tealD, margin: 0, valign: "middle",
    });
    s.addText(m[1], {
      x: x + 0.24, y: y + 0.52, w: mw - 0.46, h: 0.88,
      fontFace: F.cn, fontSize: 10, color: C.muted, margin: 0, valign: "top",
    });
  });
  s.addNotes("医防融合是「以治病为中心」转向「以健康为中心」的抓手，也是省级考核最看重的一块。");
}

/* ══════════════════════════════════════════════════════════════
   11 · 场景四：运营监管与决策（含原生图表）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "03 · 核心场景四",
    title: "事前预警 · 事中控制 · 事后评价",
    sub: "驾驶舱指标库直接内置《监测指标体系（2024版）》，自动采集、按月研判，指标卡与预警可点击下钻到明细",
  });

  const lw = 5.5;
  card(s, M, y0 + 0.16, lw, 4.24, { fill: C.white });
  s.addText("14 项二级监测指标的五个维度", {
    x: M + 0.3, y: y0 + 0.34, w: lw - 0.6, h: 0.36,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.text, margin: 0, valign: "middle",
  });
  s.addChart(
    pres.ChartType.bar,
    [{
      name: "二级指标数",
      labels: ["紧密型", "同质化", "促分工", "提效能", "保健康"],
      values: [3, 3, 3, 3, 2],
    }],
    {
      x: M + 0.24, y: y0 + 0.78, w: lw - 0.5, h: 2.5,
      barDir: "bar",
      chartColors: [C.teal],
      showTitle: false,
      showLegend: false,
      showValue: true,
      dataLabelPosition: "outEnd",
      dataLabelColor: C.text,
      dataLabelFontFace: F.num,
      dataLabelFontSize: 11,
      catAxisLabelColor: C.text,
      catAxisLabelFontFace: F.cn,
      catAxisLabelFontSize: 11,
      valAxisLabelColor: C.muted,
      valAxisLabelFontSize: 9,
      valAxisMaxVal: 4,
      valGridLine: { color: "E1EAE8", size: 1 },
      catGridLine: { style: "none" },
      barGapWidthPct: 60,
    }
  );
  s.addText("每项指标同时给出分子、分母与统计口径，未采集的不进分母——数字要能被追问", {
    x: M + 0.3, y: y0 + 3.5, w: lw - 0.6, h: 0.6,
    fontFace: F.cn, fontSize: 10.5, color: C.muted, margin: 0, valign: "top",
  });

  const rx = M + lw + 0.34, rw = CW - lw - 0.34;
  const feats = [
    ["绩效统一考核", "按机构自动汇算五维评分与排名，指标可调权；结果与公卫经费、医保支付、绩效工资总量测算挂钩"],
    ["医保基金总额付费", "基金池 → 预付批次 → 月度预结（账面对冲，不产生资金流）→ 年终清算 → 按公式分配结余；分配依据是冻结的绩效得分快照，超支只记录不自动扣减"],
    ["财务与成本核算", "会计凭证借贷平衡强校验、过账锁定与试算平衡；科室直接成本归集、分摊规则、诊次成本与床日成本"],
    ["运行监控与审计", "环境概览、接口调用统计与慢请求样本、集群心跳；写操作审计哈希链与审计统计趋势"],
  ];
  const fh2 = 0.92;
  feats.forEach((f, i) => {
    const y = y0 + 0.16 + i * (fh2 + 0.14);
    card(s, rx, y, rw, fh2, { fill: C.white });
    s.addText(f[0], {
      x: rx + 0.26, y: y + 0.12, w: rw - 0.5, h: 0.3,
      fontFace: F.cn, fontSize: 12.5, bold: true, color: C.tealD, margin: 0, valign: "middle",
    });
    s.addText(f[1], {
      x: rx + 0.26, y: y + 0.42, w: rw - 0.5, h: 0.44,
      fontFace: F.cn, fontSize: 10, color: C.muted, margin: 0, valign: "top",
    });
  });
  s.addNotes("图表数据来源：《监测指标体系（2024版）》5 个一级指标下共 14 项二级指标。总额付费那一条是与竞品拉开差距的细节——结余分配依据冻结快照，事后改分数不影响已分配结果。");
}

/* ══════════════════════════════════════════════════════════════
   12 · 拳头产品：全域慢专病（暗场）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  const y0 = head(s, {
    dark: true,
    kicker: "04 · 拳头产品",
    title: "全域慢专病全流程管理 · 十一端一体",
    sub: "依据招标文件十一个端共 163 条技术服务要求建设，逐条对照表可直接用于投标响应与验收核对",
  });

  const st = [["163", "条", "需求逐条对照"], ["239", "个", "子系统接口"], ["60", "张", "子系统数据表"], ["11", "个", "使用端"]];
  const sw = (CW - 3 * 0.3) / 4;
  st.forEach((v, i) => {
    const x = M + i * (sw + 0.3);
    card(s, x, y0 + 0.12, sw, 1.2, { fill: C.ink2, shadow: false });
    s.addText([
      { text: v[0], options: { fontFace: F.num, fontSize: 32, bold: true, color: C.amber } },
      { text: " " + v[1], options: { fontFace: F.cn, fontSize: 12, color: C.mint } },
    ], { x: x + 0.26, y: y0 + 0.28, w: sw - 0.5, h: 0.52, margin: 0, valign: "middle" });
    s.addText(v[2], {
      x: x + 0.26, y: y0 + 0.82, w: sw - 0.5, h: 0.34,
      fontFace: F.cn, fontSize: 11, color: C.mutedD, margin: 0, valign: "top",
    });
  });

  const ends = [
    "平台管理端（运行中枢）", "卫健管理端（决策监管）", "专病专家端（临床指导）", "全程管理中心端（统筹调度）",
    "服务团队专家端", "服务团队成员端", "个案管理师端", "村医端（移动）",
    "患者移动端", "智能随访服务端", "智能辅助应用端",
  ];
  const ew = (CW - 3 * 0.22) / 4, eh = 0.54;
  ends.forEach((e, i) => {
    const col = i % 4, row = Math.floor(i / 4);
    const x = M + col * (ew + 0.22), y = y0 + 1.52 + row * (eh + 0.16);
    card(s, x, y, ew, eh, { fill: C.ink2, shadow: false });
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.2, y: y + 0.21, w: 0.14, h: 0.14, fill: { color: C.tealL }, line: { type: "none" },
    });
    s.addText(e, {
      x: x + 0.44, y, w: ew - 0.6, h: eh,
      fontFace: F.cn, fontSize: 11, color: C.white, margin: 0, valign: "middle",
    });
  });

  card(s, M, y0 + 1.52 + 3 * (eh + 0.16) + 0.1, CW, 0.84, { fill: C.tealD, shadow: false });
  s.addText([
    { text: "以「可装卸子系统」形态装在平台内：", options: { fontFace: F.cn, fontSize: 12, bold: true, color: C.white } },
    { text: "独立包、单向依赖、自成迁移分支，边界由 AST 静态测试强制——既能与平台一体交付（共用患者、机构、任务、消息、考核底座），也能作为独立产品单独售卖。", options: { fontFace: F.cn, fontSize: 12, color: C.mint } },
  ], { x: M + 0.3, y: y0 + 1.52 + 3 * (eh + 0.16) + 0.1, w: CW - 0.6, h: 0.84, margin: 0, valign: "middle" });
  s.addNotes("商业意义：慢专病既是平台的一个模块，也是一个可独立投标的产品。76 处外键保证它与平台共用底座，AST 测试保证它拆得下来。");
}

/* ══════════════════════════════════════════════════════════════
   13 · 慢专病业务闭环
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "04 · 业务闭环",
    title: "「筛出来」≠「管起来」：八段闭环，每段都有数",
    sub: "筛查、目标池、纳管是三张表三个阶段，中间可停可退，所以筛查人数与纳管人数永远是两个数，不混着报",
  });
  const steps = [
    ["纳排与目标", "病种纳入/排除规则、量化与定性管理目标、阶段与关键节点可配置"],
    ["筛查与复核", "机会性 / 主动 / 居民自查 / 数据比对四类筛查，高风险复核"],
    ["目标池分发", "目标池分发、认领、状态流转，跨机构迁出需目标机构确认"],
    ["签约建档纳管", "团队、主管医生、个案管理师、村医与知情同意一并落地"],
    ["标准路径", "路径模板新建/复制/发布/启停，节点进入条件与超时处理"],
    ["监测与干预", "手工/设备/院内/POCT 数据落库即按目标判等级，异常自动派任务"],
    ["逐级转诊", "村医 → 服务站 → 卫生院 → 县医院，含有效就诊判定与下转随访闭环"],
    ["考核与积分", "自动取数计分、扣分明细可下钻；村医积分获取、兑换与线下核销"],
  ];
  const sw = 2.72, sg = 0.34, sh = 1.36;
  steps.forEach((st, i) => {
    const col = i % 4, row = Math.floor(i / 4);
    const x = M + col * (sw + sg), y = y0 + 0.16 + row * (sh + 0.42);
    card(s, x, y, sw, sh, { fill: C.white });
    badge(s, String(i + 1), x + 0.22, y + 0.2, 0.4, row === 0 ? C.teal : C.tealD, C.white, 12);
    s.addText(st[0], {
      x: x + 0.7, y: y + 0.18, w: sw - 0.9, h: 0.44,
      fontFace: F.cn, fontSize: 12.5, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(st[1], {
      x: x + 0.22, y: y + 0.66, w: sw - 0.44, h: 0.6,
      fontFace: F.cn, fontSize: 9.5, color: C.muted, margin: 0, valign: "top",
    });
    if (col < 3) arrow(s, x + sw, y, sg, sh);
  });
  card(s, M, y0 + 0.16 + 2 * (sh + 0.42) + 0.04, CW, 0.7, { fill: C.tint, shadow: false });
  s.addText([
    { text: "贯穿全系统的口径：", options: { fontFace: F.cn, fontSize: 11.5, bold: true, color: C.tealD } },
    { text: "① 慢病与专病并行运行、共用底座、分别统计；② 筛查 ≠ 纳管，三张表三阶段；③ 任务只有一张表——路径节点、随访、干预、评估、复诊、转诊、上报共用统一任务模型，「接收/转派/催办/超时升级/批量/导出」因此只实现一次。", options: { fontFace: F.cn, fontSize: 11, color: C.text } },
  ], { x: M + 0.3, y: y0 + 0.16 + 2 * (sh + 0.42) + 0.04, w: CW - 0.6, h: 0.7, margin: 0, valign: "middle" });
  s.addNotes("这一页讲的是产品的专业度：一个没做过这块业务的团队，会把筛查数当成纳管数报上去。");
}

/* ══════════════════════════════════════════════════════════════
   14 · 三端载体
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "02 · 使用体验",
    title: "一数多屏：管理端 · 居民端 · 医生端",
    sub: "三套界面同源内嵌，同一份数据、同一套权限；前端免构建，部署即用，不依赖打包工具链",
  });
  const ends = [
    ["管理端 SPA", "89", "个页面 · 8 个导航分组", [
      "总览与决策驾驶舱、基础平台、业务协同",
      "医防融合、便民惠民、全域慢专病",
      "综合管理、系统管理",
      "每个后端业务模块都有对应入口（有防回退测试）",
    ], C.teal],
    ["居民端 H5", "6", "个页签 · 移动优先", [
      "微信 / 手机号验证码登录 + 实名绑定",
      "家庭成员代管与档案切换",
      "自助预约、住院与费用清单、手术安排",
      "签约 / 账单 / 转诊查询、慢专病自我管理、站内消息",
    ], C.tealD],
    ["医生移动端 H5", "8", "个页签 · 查房即录", [
      "待办收件箱、危急值确认与处置",
      "待审检查申请领取与出报告",
      "查房（病程记录 / 体征 / 文书完整性）、手术排班与术中记录",
      "慢病随访录入、慢专病待办、患者档案速查",
    ], C.teal],
  ];
  const cw = (CW - 2 * 0.34) / 3, ch = 3.62;
  ends.forEach((e, i) => {
    const x = M + i * (cw + 0.34), y = y0 + 0.16;
    card(s, x, y, cw, ch, { fill: C.white });
    s.addText(e[0], {
      x: x + 0.28, y: y + 0.26, w: cw - 0.56, h: 0.4,
      fontFace: F.cn, fontSize: 16, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText([
      { text: e[1], options: { fontFace: F.num, fontSize: 30, bold: true, color: e[4] } },
      { text: "  " + e[2], options: { fontFace: F.cn, fontSize: 10.5, color: C.muted } },
    ], { x: x + 0.28, y: y + 0.7, w: cw - 0.56, h: 0.56, margin: 0, valign: "bottom" });
    bullets(s, e[3], x + 0.28, y + 1.42, cw - 0.54, ch - 1.6, { size: 10.5, color: C.text, gap: 8 });
  });
  pageNote(s, "端到端测试用真实浏览器驱动关键链路：登录 → 驾驶舱 → 开单 → 出报告（危急值）→ 确认接收 → 处置闭环，以及住院文书、手术全流程与医生移动端登录。");
  s.addNotes("免构建 SPA 是刻意的取舍：县域实施环境复杂，少一个前端构建链就少一类现场故障。");
}

/* ══════════════════════════════════════════════════════════════
   15 · 技术架构
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "05 · 技术架构",
    title: "一个进程装得下，一套数据说得清",
    sub: "单进程 FastAPI 单体 + 可装卸子系统：县域体量下最省运维的形态，也保留了拆分的接缝",
  });

  // 左：运行拓扑
  const lw = 6.4;
  card(s, M, y0 + 0.16, lw, 4.5, { fill: C.white });
  s.addText("运行拓扑", {
    x: M + 0.3, y: y0 + 0.34, w: lw - 0.6, h: 0.34,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.text, margin: 0, valign: "middle",
  });
  const boxes = [
    ["浏览器 / H5 / 小程序", C.tint, C.text, 0.62],
    ["Uvicorn + FastAPI 应用进程", C.teal, C.white, 1.44],
    ["PostgreSQL 16  ·  Redis 7（可选）", C.ink, C.white, 0.62],
  ];
  let by = y0 + 0.82;
  boxes.forEach((b, i) => {
    card(s, M + 0.3, by, lw - 0.6, b[3], { fill: b[1], shadow: false });
    s.addText(b[0], {
      x: M + 0.44, y: by + (i === 1 ? 0.1 : 0), w: lw - 0.88, h: i === 1 ? 0.42 : b[3],
      fontFace: F.cn, fontSize: 12.5, bold: true, color: b[2],
      align: i === 1 ? "left" : "center", valign: "middle", margin: 0,
    });
    if (i === 1) {
      const inner = [
        "中间件：安全响应头 → 结构化请求日志 → 审计落库",
        "约 90 个路由模块  +  register_spd() 装载慢专病子系统",
        "WebSocket 实时预警  ·  进程内定时调度器  ·  领域事件总线",
      ];
      s.addText(inner.map((t, k) => ({ text: t, options: { breakLine: k !== inner.length - 1 } })), {
        x: M + 0.5, y: by + 0.54, w: lw - 1.0, h: 0.82,
        fontFace: F.cn, fontSize: 9.5, color: C.mint, lineSpacingMultiple: 1.3, margin: 0, valign: "top",
      });
    }
    by += b[3] + 0.16;
  });
  s.addText("外部通道均为双态设计（短信 / 微信 / 外呼 / 支付）：开发演示走本地通道，接真实通道只改环境变量，业务代码不动。", {
    x: M + 0.3, y: by + 0.04, w: lw - 0.6, h: 0.56,
    fontFace: F.cn, fontSize: 10, color: C.muted, margin: 0, valign: "top",
  });

  // 右：技术栈与关键取舍
  const rx = M + lw + 0.34, rw = CW - lw - 0.34;
  const rows = [
    ["Web / ORM", "FastAPI + Uvicorn · SQLAlchemy 2.x"],
    ["数据库", "PostgreSQL 16（开发 SQLite）· Redis 7 可选"],
    ["结构迁移", "Alembic 双 head（平台链 + 慢专病链），89 个迁移全部实现回退"],
    ["前端", "原生 JS 免构建 SPA，三端同源内嵌"],
    ["认证", "自制 JWT（HMAC-SHA256 / 国密 SM3），员工端与居民端双身份 scope 隔离"],
  ];
  card(s, rx, y0 + 0.16, rw, 2.42, { fill: C.white });
  s.addText("技术栈", {
    x: rx + 0.28, y: y0 + 0.32, w: rw - 0.56, h: 0.32,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.text, margin: 0, valign: "middle",
  });
  rows.forEach((r, i) => {
    const y = y0 + 0.72 + i * 0.34;
    s.addText(r[0], {
      x: rx + 0.28, y, w: 1.3, h: 0.32,
      fontFace: F.cn, fontSize: 10, bold: true, color: C.tealD, margin: 0, valign: "middle",
    });
    s.addText(r[1], {
      x: rx + 1.6, y, w: rw - 1.9, h: 0.32,
      fontFace: F.cn, fontSize: 10, color: C.text, margin: 0, valign: "middle",
    });
  });
  card(s, rx, y0 + 2.74, rw, 1.92, { fill: C.tint, shadow: false });
  s.addText("三个写进代码的关键取舍", {
    x: rx + 0.28, y: y0 + 2.86, w: rw - 0.56, h: 0.3,
    fontFace: F.cn, fontSize: 12, bold: true, color: C.tealD, margin: 0, valign: "middle",
  });
  bullets(s, [
    "金额一律定点数 Numeric(14,2)，换库不会「同一笔账算出两个数」",
    "领域事件同事务同步发布，不引入队列，避免最终一致性带来的对账工作",
    "子系统单向依赖、可装卸，边界由静态测试守住，不靠口头约定",
  ], rx + 0.28, y0 + 3.2, rw - 0.54, 1.36, { size: 10, color: C.text, gap: 6 });
  s.addNotes("架构问答准备：为什么是单体？县域体量下单体的运维成本最低，且我们保留了接缝（子系统单向依赖 + 事件总线），需要拆时能拆。多实例部署配 Redis 即可共享登出黑名单、限流与任务锁。");
}

/* ══════════════════════════════════════════════════════════════
   16 · 工程质量与治理（暗场）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  const y0 = head(s, {
    dark: true,
    kicker: "05 · 工程质量",
    title: "交付得出去，也维护得下去",
    sub: "医疗系统真正的成本在交付之后。这套平台把「不退步」做成了机制，而不是口号",
  });

  const lw = 5.9;
  s.addText("六项 CI 门禁，全部阻断", {
    x: M, y: y0 + 0.1, w: lw, h: 0.34,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.white, margin: 0, valign: "middle",
  });
  const gates = [
    ["字节编译 + 迁移图校验", "双 head 拓扑正确，迁移可执行"],
    ["ruff 代码规范", "存量清零，新增报错即拦截"],
    ["mypy 静态类型", "存量清零；先跑环境探针，杜绝「假绿」"],
    ["单元 + 冒烟测试", "进程内 SQLite 快速套件 + 应用可启动"],
    ["集成测试（真 PostgreSQL）", "空库跑通迁移，逐表逐列比对模型"],
    ["覆盖率门禁", "阈值 70%，低于即阻断构建"],
  ];
  gates.forEach((g, i) => {
    const y = y0 + 0.52 + i * 0.62;
    card(s, M, y, lw, 0.54, { fill: C.ink2, shadow: false });
    badge(s, "✓", M + 0.16, y + 0.07, 0.4, C.teal, C.white, 12);
    s.addText(g[0], {
      x: M + 0.66, y, w: 2.5, h: 0.54,
      fontFace: F.cn, fontSize: 11, bold: true, color: C.white, margin: 0, valign: "middle",
    });
    s.addText(g[1], {
      x: M + 3.2, y, w: lw - 3.36, h: 0.54,
      fontFace: F.cn, fontSize: 9.5, color: C.mutedD, margin: 0, valign: "middle",
    });
  });

  const rx = M + lw + 0.34, rw = CW - lw - 0.34;
  s.addText("治理棘轮：基线只许调小，绝不许变大", {
    x: rx, y: y0 + 0.1, w: rw, h: 0.34,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.white, margin: 0, valign: "middle",
  });
  const ratchets = [
    ["核心数据不可变", "一个核心概念只有一张权威表，身份字段单一归属，另造平行主数据即报错"],
    ["核心表冻结", "5 张核心表的列集合快照锁定，改列须先写架构决策记录（ADR）"],
    ["接口契约棘轮", "新端点漏声明响应契约即报错，欠账只许变小"],
    ["子系统边界", "AST 扫描强制单向依赖与独立迁移分支，破边界即报错"],
    ["并发写入防复发", "写唯一约束表的接口必须处理冲突，静态用例逐个盯"],
    ["迁移数据安全", "迁移不得静默改动存量业务数据，AST 扫描全部迁移"],
  ];
  ratchets.forEach((r, i) => {
    const y = y0 + 0.52 + i * 0.62;
    card(s, rx, y, rw, 0.54, { fill: C.ink2, shadow: false });
    s.addText(r[0], {
      x: rx + 0.24, y, w: 1.9, h: 0.54,
      fontFace: F.cn, fontSize: 11, bold: true, color: C.amber, margin: 0, valign: "middle",
    });
    s.addText(r[1], {
      x: rx + 2.16, y, w: rw - 2.36, h: 0.54,
      fontFace: F.cn, fontSize: 9, color: C.mutedD, margin: 0, valign: "middle",
    });
  });
  pageNote(s, "配套资产：六张架构地图（架构 / 模块 / 数据 / 接口 / 依赖 / 技术债）+ 14 份架构决策记录 + 用户手册、运维手册、接口对接规范、培训手册，交付即可移交。", true);
  s.addNotes("对投资方：这是「能不能长期演进」的证据。对客户：这是「换人也接得住」的证据——文档与守卫都在仓库里，不在某个人脑子里。");
}

/* ══════════════════════════════════════════════════════════════
   17 · 安全合规与信创
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "05 · 安全合规",
    title: "医疗数据的底线，写进测试里",
    sub: "落实网络安全等级保护与商用密码应用要求，个人隐私保护贯穿采集、存储、查询、出口、审计全链路",
  });
  const items = [
    ["身份与权限", "六类内置角色 RBAC + 从路由表自动登记的自定义权限点；员工端与居民端两套身份共用格式、靠 scope 双向拒绝；登出令牌黑名单与登录防爆破锁定"],
    ["横向数据隔离", "机构维度三档可见性（可见 / 可统计 / 可写）+ 患者维度七类业务关系推导；判定与留痕在同一次调用里完成，不存在「查了没记」"],
    ["隐私保护", "身份证号 / 手机号支持列加密存储（SM4-CTR + HMAC 检索索引），出口统一脱敏；等值检索强制走加密安全通道，裸写查询由静态用例拦截"],
    ["审计留痕", "所有写操作进审计哈希链，敏感读进调阅日志；审计统计给出趋势、失败码分布与高频操作，可追溯到人"],
    ["信创适配", "46 个金额列改定点数，国产库聚合精确；可移植性护栏钉住方言差异；国密 SM3 用于散列与 MAC，SM2/SM4 建议接硬件密码机而非自实现"],
    ["备份与容灾", "库 + 附件 + 密钥指纹一体备份，配套恢复与恢复演练脚本；生产凭据强度守卫，弱口令与占位符密钥直接拒绝启动"],
  ];
  const cw = (CW - 2 * 0.3) / 3, ch = 1.72;
  items.forEach((it, i) => {
    const col = i % 3, row = Math.floor(i / 3);
    const x = M + col * (cw + 0.3), y = y0 + 0.16 + row * (ch + 0.26);
    card(s, x, y, cw, ch, { fill: C.white });
    badge(s, String(i + 1), x + 0.26, y + 0.24, 0.42, C.tealD, C.white, 12);
    s.addText(it[0], {
      x: x + 0.78, y: y + 0.22, w: cw - 1.0, h: 0.44,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.text, margin: 0, valign: "middle",
    });
    s.addText(it[1], {
      x: x + 0.26, y: y + 0.74, w: cw - 0.5, h: 0.86,
      fontFace: F.cn, fontSize: 9.5, color: C.muted, margin: 0, valign: "top",
    });
  });
  pageNote(s, "如实说明：等保测评、密评与异地灾备属部署期工作，需在目标环境完成；平台侧已做完的与部署期须做的，在《信创适配与备份容灾》文档中分开列明，不混为一谈。");
  s.addNotes("坦诚是加分项：明确区分「平台已做完」与「部署期要做」，客户和投资方都会更信任其余的陈述。");
}

/* ══════════════════════════════════════════════════════════════
   18 · 集成对接与实施交付
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "06 · 交付能力",
    title: "不做孤岛：进得来、出得去、落得下",
    sub: "县域现场从来不是空地——存量 HIS、LIS、公卫系统与省市平台都要接。集成能力是实施周期的决定因素",
  });
  const lw = (CW - 0.34) / 2;
  const groups = [
    ["对接能力", C.teal, [
      "HL7 v2 ADT 与 FHIR R4（Patient · Observation）入站转换与导出",
      "集成平台 ESB：接入方注册（令牌 + 限流）、消息队列重试与死信、流程编排、成功率与积压统计",
      "多源数据采集器与同步监控：成功时间、数据量、延迟、成功率、运行状态",
      "统一编码字典与批量导入，内置常用 ICD-10 诊断与药品目录",
      "外部通道双态：短信 / 微信 / 外呼 / 支付网关，配置即切换",
    ]],
    ["实施交付", C.tealD, [
      "docker compose 一键起 应用 + PostgreSQL 16 + Redis 7，容器启动自动执行结构迁移",
      "存量数据 CSV 批量导入，支持 dry-run 校验模式与错误行明细",
      "演示数据一键灌入，覆盖全部模块，脚本末尾自带终态自检",
      "压测基线脚本：7 个核心场景的 P50 / P95 / QPS，可作容量规划依据",
      "文档体系：用户手册、运维手册、接口对接规范、培训手册、系统功能清单",
    ]],
  ];
  groups.forEach((g, i) => {
    const x = M + i * (lw + 0.34), y = y0 + 0.16;
    card(s, x, y, lw, 3.72, { fill: C.white });
    s.addShape(pres.ShapeType.roundRect, {
      x: x + 0.28, y: y + 0.3, w: 1.5, h: 0.44, rectRadius: 0.08,
      fill: { color: g[1] }, line: { type: "none" },
    });
    s.addText(g[0], {
      x: x + 0.28, y: y + 0.3, w: 1.5, h: 0.44,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.white,
      align: "center", valign: "middle", margin: 0,
    });
    bullets(s, g[2], x + 0.28, y + 0.98, lw - 0.56, 2.6, { size: 11, color: C.text, gap: 10 });
  });
  pageNote(s, "分期建议：第一期夯基达标（基础平台 + 五个核心共享中心 + 中心药房 + 互认 + 档案开放）；第二期扩面协同；第三期智能提升。");
  s.addNotes("实施是县域项目最容易翻车的环节。强调 dry-run 导入与终态自检脚本——这些细节说明团队真的做过现场。");
}

/* ══════════════════════════════════════════════════════════════
   19 · 客户价值
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(false);
  const y0 = head(s, {
    kicker: "06 · 客户价值",
    title: "惠民 · 惠医 · 惠政",
    sub: "同一套系统，对三类使用者给出三种可被验收的结果",
  });
  const vals = [
    ["惠民", "群众", C.teal, [
      "一卡（码）通用，检查检验少跑腿、少重复付费",
      "预约、住院费用清单、手术安排、账单转诊掌上可查",
      "电子健康档案向本人开放，家庭成员可代管",
      "慢病管理到家：监测、随访、干预、宣教线上线下一体",
    ]],
    ["惠医", "医务人员", C.tealD, [
      "基层检查、上级诊断、结果互认，优质资源真正下沉",
      "危急值秒级触达并留痕，待办与随访自动派生，不靠人记",
      "查房、手术、随访在移动端即录即存，减少二次录入",
      "审方与规则引擎兜住用药风险，减少事后追责",
    ]],
    ["惠政", "卫健与医共体管理层", C.teal, [
      "人财物统一监管，14 项监测指标自动出数、可下钻",
      "绩效考核数据化，与公卫经费、医保支付、薪酬测算挂钩",
      "医保基金效能可监测，总额付费与结余分配有据可依",
      "传染病与公卫风险多点触发预警，应急有指挥载体",
    ]],
  ];
  const cw = (CW - 2 * 0.34) / 3, ch = 3.66;
  vals.forEach((v, i) => {
    const x = M + i * (cw + 0.34), y = y0 + 0.16;
    card(s, x, y, cw, ch, { fill: C.white });
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.28, y: y + 0.28, w: 0.72, h: 0.72, fill: { color: v[2] }, line: { type: "none" },
    });
    s.addText(v[0], {
      x: x + 0.28, y: y + 0.28, w: 0.72, h: 0.72,
      fontFace: F.cn, fontSize: 17, bold: true, color: C.white,
      align: "center", valign: "middle", margin: 0,
    });
    s.addText(v[1], {
      x: x + 1.12, y: y + 0.28, w: cw - 1.4, h: 0.72,
      fontFace: F.cn, fontSize: 12, color: C.muted, margin: 0, valign: "middle",
    });
    bullets(s, v[3], x + 0.28, y + 1.22, cw - 0.56, ch - 1.4, { size: 11, color: C.text, gap: 11 });
  });
  s.addNotes("三个视角对应三类决策人：群众满意度进考核、医务人员的使用率决定系统活不活、管理层是签字的人。");
}

/* ══════════════════════════════════════════════════════════════
   20 · 投资亮点与路线图（暗场）
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  const y0 = head(s, {
    dark: true,
    kicker: "06 · 投资亮点",
    title: "可复制 · 可交付 · 可扩展",
    sub: "县域市场的竞争不在功能清单长度，而在「能不能按期验收、能不能持续演进」",
  });
  const lw = 6.4;
  const points = [
    ["政策确定性", "36 项功能逐项对照、慢专病 163 条逐条对照——招标条目可直接响应，投标不做二次翻译"],
    ["产品完整度", "946 个接口覆盖 90 个业务域，是县域全场景平台而非单点工具，替换成本高、粘性强"],
    ["可装卸子系统", "慢专病既可随平台整体交付，也可作为独立产品单独售卖；边界由测试守住，不是营销话术"],
    ["工程可维护性", "2,360 项测试 + 六项阻断门 + 治理棘轮，交付后仍能安全迭代，长期服务成本可控"],
  ];
  points.forEach((p, i) => {
    const y = y0 + 0.14 + i * 1.02;
    card(s, M, y, lw, 0.9, { fill: C.ink2, shadow: false });
    badge(s, String(i + 1), M + 0.22, y + 0.23, 0.44, C.amber, C.ink, 13);
    s.addText(p[0], {
      x: M + 0.82, y: y + 0.12, w: lw - 1.04, h: 0.32,
      fontFace: F.cn, fontSize: 13, bold: true, color: C.white, margin: 0, valign: "middle",
    });
    s.addText(p[1], {
      x: M + 0.82, y: y + 0.44, w: lw - 1.04, h: 0.4,
      fontFace: F.cn, fontSize: 10, color: C.mutedD, margin: 0, valign: "top",
    });
  });

  const rx = M + lw + 0.34, rw = CW - lw - 0.34;
  s.addText("建设分期路线", {
    x: rx, y: y0 + 0.14, w: rw, h: 0.34,
    fontFace: F.cn, fontSize: 14, bold: true, color: C.white, margin: 0, valign: "middle",
  });
  const phases = [
    ["第一期", "夯基达标（第 1 年）", "基础平台与数据中心、统一编码与主索引、五个核心共享中心与中心药房、结果互认与档案开放、等保与灾备"],
    ["第二期", "扩面协同（第 2 年）", "病理 / 消供 / 急救中心上线，远程医疗延伸至村室，便民惠民九项与医防融合全面上线，人财物与绩效、驾驶舱投运"],
    ["第三期", "智能提升（第 3 年起）", "AI 辅助诊断、智能审方、多点触发预警深化，用药监测与健康画像，对标监测指标滚动优化"],
  ];
  phases.forEach((p, i) => {
    const y = y0 + 0.58 + i * 1.24;
    card(s, rx, y, rw, 1.1, { fill: C.ink2, shadow: false });
    s.addText([
      { text: p[0], options: { fontFace: F.cn, fontSize: 12, bold: true, color: C.amber } },
      { text: "   " + p[1], options: { fontFace: F.cn, fontSize: 12, bold: true, color: C.white } },
    ], { x: rx + 0.26, y: y + 0.12, w: rw - 0.5, h: 0.34, margin: 0, valign: "middle" });
    s.addText(p[2], {
      x: rx + 0.26, y: y + 0.46, w: rw - 0.5, h: 0.56,
      fontFace: F.cn, fontSize: 9.5, color: C.mutedD, margin: 0, valign: "top",
    });
  });
  s.addNotes("投资方问答准备：护城河是「政策对照 + 完整度 + 工程规范」三件事的叠加，任何一项单独都不难，凑齐了很难在短期内被复制。");
}

/* ══════════════════════════════════════════════════════════════
   21 · 封底
   ══════════════════════════════════════════════════════════════ */
{
  const s = newSlide(true);
  s.addShape(pres.ShapeType.ellipse, {
    x: -2.2, y: 4.5, w: 5.6, h: 5.6, fill: { color: C.teal, transparency: 82 }, line: { type: "none" },
  });
  s.addShape(pres.ShapeType.ellipse, {
    x: 10.0, y: -1.2, w: 4.6, h: 4.6, fill: { color: C.tealL, transparency: 84 }, line: { type: "none" },
  });
  s.addText("一家人 · 一盘棋 · 一本账", {
    x: M, y: 2.5, w: CW, h: 1.0,
    fontFace: F.cn, fontSize: 40, bold: true, color: C.white, margin: 0, valign: "middle",
  });
  s.addText("县域医共体信息化平台 medplat　·　全域慢专病全流程管理子系统", {
    x: M, y: 3.6, w: CW, h: 0.42,
    fontFace: F.cn, fontSize: 15, color: C.mint, margin: 0, valign: "middle",
  });
  s.addText("欢迎实地演示与逐条走查：功能对照表、系统功能清单、接口文档与测试报告均可现场打开复核。", {
    x: M, y: 4.24, w: 9.6, h: 0.42,
    fontFace: F.cn, fontSize: 12, color: C.white, margin: 0, valign: "middle",
  });
  s.addText("依据：国卫办规划函〔2025〕63号《紧密型县域医共体信息化功能指引》等文件建设", {
    x: M, y: 6.72, w: CW, h: 0.32,
    fontFace: F.cn, fontSize: 10, color: C.mutedD, margin: 0, valign: "middle",
  });
  s.addNotes("收尾：邀请对方提出任意一条招标要求，现场在对照表里找到落点并打开对应接口——这是最有说服力的结束方式。");
}

pres.writeFile({ fileName: OUT }).then(() => console.log("已生成：" + OUT));
