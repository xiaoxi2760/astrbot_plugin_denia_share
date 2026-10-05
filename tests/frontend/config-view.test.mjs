/**
 * 配置页回归测试（从仓库根目录的 _verify_denia_webui.mjs 迁入）。
 *
 * 核心回归点：改配置项时**不能整页重绘**。
 * 早期实现每次输入都重建 DOM，正在输入的文本框会被换掉 → 焦点丢失、光标跳到
 * 末尾，用户根本没法正常填 Token。断言方式是「输入前后拿到的是同一个 DOM 节点
 * 对象」，比断言 className 之类更直接。
 */
import assert from "node:assert/strict";
import test from "node:test";

import { El, installDom } from "./dom-stub.mjs";

const PAGE = new URL("../../pages/denia/", import.meta.url);

let createConfigView;
let container;

test.before(async () => {
  const env = installDom();
  ({ createConfigView } = await import(new URL("views/config.js", PAGE).href));
  container = new El("div");
  return () => env.teardown();
});

function makeCtx() {
  const payload = {
    sections: [{ key: "net", label: "网络", description: "", groups: ["接口"] }],
    groups: [{ name: "接口", description: "", keys: ["API_TOKEN", "ENABLE_X"] }],
    items: [
      {
        key: "API_TOKEN",
        label: "接口 Token",
        group: "接口",
        type: "string",
        hint: "",
        placeholder: "",
      },
      { key: "ENABLE_X", label: "启用某功能", group: "接口", type: "bool", hint: "" },
    ],
    platforms: [],
    problems: [],
    values: { API_TOKEN: "", ENABLE_X: false },
  };
  return {
    api: {
      get: async (path) => (path === "config" ? payload : { version: "1.0.0" }),
      post: async () => ({ values: {}, changed: [], errors: [] }),
    },
    setHead: () => {},
    setVersion: () => {},
  };
}

const textInputs = () =>
  container._find((n) => n.tagName === "INPUT" && n.attributes.type === "text", []);
const checkboxes = () =>
  container._find((n) => n.tagName === "INPUT" && n.attributes.type === "checkbox", []);

const submit = () => container.querySelector("[data-save-submit]");
const discard = () => container.querySelector("[data-save-discard]");
const badge = () => container.querySelectorAll("[data-dirty-scope]")[0];
const saveText = () => container.querySelector("[data-save-text]");

test("配置页：脏状态随输入变化，且不打断正在编辑的输入框", async () => {
  const view = createConfigView(makeCtx());
  await view.mount(container);

  assert.equal(submit().disabled, true, "初始保存按钮应禁用");
  assert.equal(discard().disabled, true, "初始放弃按钮应禁用");
  assert.equal(badge().hidden, true, "初始徽标应隐藏");

  const inputs = textInputs();
  assert.equal(inputs.length, 1, `应渲染出 1 个 token 文本框，实际 ${inputs.length}`);
  const input = inputs[0];

  input.dispatch("input", { target: Object.assign(input, { value: "tok_abc123" }) });
  assert.equal(submit().disabled, false, "输入后保存按钮应立即可用");
  assert.ok(submit().textContent.includes("（1）"), `保存按钮应带计数：${submit().textContent}`);
  assert.ok(saveText().textContent.includes("已修改 1 项"), saveText().textContent);
  assert.equal(badge().hidden, false);
  assert.ok(badge().textContent.startsWith("1"), badge().textContent);

  // 关键回归：输入后拿到的必须还是同一个节点
  assert.equal(textInputs()[0], input, "输入过程中文本框被重建了（会丢焦点）");

  // 改回原值 → 脏状态消失
  input.dispatch("input", { target: Object.assign(input, { value: "" }) });
  assert.equal(submit().disabled, true, "改回原值后应重新禁用");
  assert.equal(badge().hidden, true);
});

test("配置页：拨动开关同样只改脏状态，不重建文本框", async () => {
  const view = createConfigView(makeCtx());
  await view.mount(container);

  const input = textInputs()[0];
  const checkbox = checkboxes()[0];
  assert.ok(checkbox, "应渲染出开关");

  checkbox.checked = true;
  checkbox.dispatch("change", { target: checkbox });

  assert.equal(submit().disabled, false, "拨动开关后保存按钮应可用");
  assert.equal(textInputs()[0], input, "开关切换重建了文本框");
});
