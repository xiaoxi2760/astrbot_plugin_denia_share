/**
 * 数字型配置项的回归测试。
 *
 * ## 为什么单独写
 *
 * 用户报的现象是：「同群重复链接去重」那一项**既不显示默认值 60、也点不进去**。
 * 两个原因叠在一起：
 *
 * 1. **值缺失渲染成空框**：`value === null || value === undefined ? "" : ...`。
 *    新增的配置项在用户保存过一次之前，配置文件里没有它 → 空框。
 * 2. **空数字框会被挤塌**：`.field-body` 是 flex，`<input>` 的 min-content 宽度是 0，
 *    旁边那段长提示文字（这一项的提示特别长）把它压成**只剩微调箭头的小方块**。
 *
 * 合起来就是「一个看起来坏了的控件」：没有默认值、也点不进去。而且清空过一次就
 * 永久停在这个状态，「已改」还亮着，用户没法从界面自救。
 *
 * 断言三件事：空值时 placeholder 显示默认值、控件不会被挤塌、清空输入不算改动
 * 且会把框里的字恢复回去。
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

import { El, installDom } from "./dom-stub.mjs";

const PAGE = new URL("../../pages/denia/", import.meta.url);
const STYLESHEET = new URL("../../pages/denia/style.css", import.meta.url);

let createConfigView;
let container;

const ITEM = {
  key: "DUPLICATE_LINK_WINDOW_SECONDS",
  label: "同群重复链接去重",
  group: "维护",
  subgroup: "缓存清理",
  type: "int",
  default: 60,
  min: 0,
  max: 3600,
  unit: "秒",
  hint: "很长的一段提示文字，用来在真实界面里把控件挤到只剩微调箭头",
};

test.before(async () => {
  const env = installDom();
  ({ createConfigView } = await import(new URL("views/config.js", PAGE).href));
  container = new El("div");
  return () => env.teardown();
});

function makeCtx(values) {
  const payload = {
    sections: [{ key: "maint", label: "维护", description: "", groups: ["维护"] }],
    groups: [{ name: "维护", description: "", keys: [ITEM.key] }],
    items: [ITEM],
    platforms: [],
    problems: [],
    values,
  };
  return {
    api: {
      get: async (p) => (p === "config" ? payload : { version: "1.0.0" }),
      post: async () => ({ values: {}, changed: [], errors: [] }),
    },
    setHead: () => {},
    setVersion: () => {},
  };
}

const numInput = () =>
  container._find((n) => n.tagName === "INPUT" && n.attributes.type === "number", []);
const submit = () => container.querySelector("[data-save-submit]");
const dirtyMark = () => container.querySelector("[data-dirty-scope]");

test("值缺失时用 placeholder 显示默认值，而不是留一个空框", async () => {
  // 模拟新增配置项：配置文件里还没有这个键
  const view = createConfigView(makeCtx({}));
  await view.mount(container);

  const input = numInput()[0];
  assert.ok(input, "应当渲染一个数字输入框");
  assert.ok(
    input.attributes.placeholder === "60",
    "拿不到值时要用默认值当 placeholder —— 空框会被长提示文字挤塌成只剩微调箭头",
  );
});

test("样式表给控件兜了 min-width，不会被长提示文字挤扁", async () => {
  // 内联 style 在 dom-stub 里存不下来，布局守卫直接校验样式表 —— 那才是真正
  // 生效的地方。少了这条，空数字框会被同排的长提示文字压成一个只剩微调箭头的
  // 小方块，用户看着就像控件坏了。
  const css = fs.readFileSync(STYLESHEET, "utf8");
  assert.match(
    css, /\.field-body\s+\.input[^{]*\{[^}]*min-width/,
    ".field-body 里的控件必须给 min-width",
  );
  assert.match(
    css, /\.field-body\s+\.field-hint[^{]*\{[^}]*min-width:\s*0/,
    "提示文字要允许收缩到 0，把宽度让给控件",
  );
});

test("清空输入不算改动，并把框里的字恢复回去", async () => {
  const view = createConfigView(makeCtx({ [ITEM.key]: 60 }));
  await view.mount(container);

  const input = numInput()[0];
  assert.equal(input.value, "60");

  assert.equal(submit().disabled, true, "初始没有改动");

  // 模拟用户把框清空（选中全删、或退格到空）
  input.dispatch("input", { target: Object.assign(input, { value: "" }) });

  assert.equal(submit().disabled, true, "清空输入被当成了改动");
  assert.equal(
    input.value, "60",
    "清空后应当把框里的字恢复成当前值，而不是留一个空框",
  );
});

test("输入合法数字照常记为改动", async () => {
  const view = createConfigView(makeCtx({ [ITEM.key]: 60 }));
  await view.mount(container);

  const input = numInput()[0];
  input.dispatch("input", { target: Object.assign(input, { value: "30" }) });

  assert.equal(submit().disabled, false, "输入 30 应当算改动");
  assert.ok(dirtyMark(), "应当存在脏状态徽标");
  assert.equal(dirtyMark().hidden, false, "应当显示「已改」");
});
