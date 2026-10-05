/**
 * 接口调用层测试：开发态 fetch 回退 + 响应解包契约。
 *
 * 这里守的是「两条路径必须等价」：有 bridge 时 api.* 走 postMessage，没有时
 * 走 fetch 直连后端。两者若对同一份响应的处理有任何差异，就会出现「本地调通、
 * 线上白屏」或者反过来——这类分叉最难在联调时发现，所以单测钉死。
 */
import assert from "node:assert/strict";
import test from "node:test";

import { installDom } from "./dom-stub.mjs";

const APP = new URL("../../pages/denia/app.js", import.meta.url);

let variant = 0;

/** 装一套 DOM 后导入一份全新的 app.js 实例（module 级会读 window/location）。 */
async function loadApp({ bridge = null, search = "" } = {}) {
  // 阻止 app.js 自动 boot()：本文件只测纯函数与 api 封装，boot 需要一整套
  // 真实的页面骨架（rail-nav / stage-body 等），那属于 index.html 集成测试。
  globalThis.__DENIA_NO_AUTOBOOT__ = true;
  const env = installDom({ search });
  if (bridge) env.window.AstrBotPluginPage = bridge;
  const url = new URL(APP);
  url.searchParams.set("v", String(++variant));
  const mod = await import(url.href);
  return { mod, env };
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => body,
  };
}

test("解包：成功时原样返回裸业务对象（后端不套信封）", async () => {
  const { mod, env } = await loadApp();
  try {
    const payload = { version: "1.2.3", platforms: [{ name: "bilibili" }] };
    assert.deepEqual(await mod.unwrapResponse(jsonResponse(payload)), payload);
  } finally {
    env.teardown();
  }
});

test("解包：status=error 必须抛错，且带上后端 message", async () => {
  const { mod, env } = await loadApp();
  try {
    await assert.rejects(
      () => mod.unwrapResponse(jsonResponse({ status: "error", message: "未知平台" }, { ok: false, status: 400 })),
      /未知平台/,
    );
  } finally {
    env.teardown();
  }
});

test("解包：非 2xx 且不是错误信封时，也不能把响应体当业务数据交出去", async () => {
  const { mod, env } = await loadApp();
  try {
    // 典型场景：反代返回 HTML 502 页
    const resp = { ok: false, status: 502, json: async () => { throw new SyntaxError("Unexpected token <"); } };
    await assert.rejects(() => mod.unwrapResponse(resp), /HTTP 502/);
  } finally {
    env.teardown();
  }
});

test("开发态：没有 bridge 时 api.get 直连后端并解包", async () => {
  const { mod, env } = await loadApp();
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse({ items: [], total: 0 });
  };
  try {
    const result = await mod.api.get("cache", { keyword: "abc", limit: 20 });
    assert.deepEqual(result, { items: [], total: 0 });
    assert.equal(calls.length, 1);
    assert.ok(calls[0].url.includes("/api/plug/astrbot_plugin_denia_share/cache"), calls[0].url);
    assert.ok(calls[0].url.includes("keyword=abc"), calls[0].url);
    assert.ok(calls[0].url.includes("limit=20"), calls[0].url);
  } finally {
    env.teardown();
  }
});

test("开发态：空值参数不写进 query（避免 ?limit= 让后端拿到空串）", async () => {
  const { mod, env } = await loadApp();
  let seen = "";
  globalThis.fetch = async (url) => {
    seen = url;
    return jsonResponse({});
  };
  try {
    await mod.api.get("cache", { keyword: "", limit: undefined, offset: null });
    assert.ok(!seen.includes("keyword="), seen);
    assert.ok(!seen.includes("limit="), seen);
    assert.ok(!seen.includes("offset="), seen);
  } finally {
    env.teardown();
  }
});

test("开发态：api.post 发 JSON 体，错误信封照抛", async () => {
  const { mod, env } = await loadApp();
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse({ status: "error", message: "没有需要保存的配置项" }, { ok: false, status: 400 });
  };
  try {
    await assert.rejects(() => mod.api.post("config", { values: {} }), /没有需要保存的配置项/);
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(calls[0].init.body), { values: {} });
  } finally {
    env.teardown();
  }
});

test("开发态：?api= 覆盖后端地址，便于指向远端 AstrBot 实例", async () => {
  const { mod, env } = await loadApp({ search: "?api=http://127.0.0.1:6180/" });
  let seen = "";
  globalThis.fetch = async (url) => {
    seen = url;
    return jsonResponse({ ok: 1 });
  };
  try {
    await mod.api.get("overview");
    assert.ok(seen.startsWith("http://127.0.0.1:6180/api/plug/astrbot_plugin_denia_share/"), seen);
  } finally {
    env.teardown();
  }
});

test("有 bridge 时必须走 bridge，不发 fetch", async () => {
  const calls = [];
  const { mod, env } = await loadApp({
    bridge: {
      apiGet: async (endpoint, params) => {
        calls.push(["get", endpoint, params]);
        return { from: "bridge" };
      },
      apiPost: async (endpoint, body) => {
        calls.push(["post", endpoint, body]);
        return { from: "bridge" };
      },
    },
  });
  globalThis.fetch = async () => {
    throw new Error("有 bridge 时不该发 fetch");
  };
  try {
    assert.deepEqual(await mod.api.get("overview"), { from: "bridge" });
    assert.deepEqual(await mod.api.post("config", { values: { A: 1 } }), { from: "bridge" });
    assert.equal(calls.length, 2);
  } finally {
    env.teardown();
  }
});
