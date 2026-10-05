/**
 * 视图 ↔ URL hash 同步测试。
 *
 * 接 hash 之前 state.view 只是个普通变量：刷新永远回总览、后退键无效、
 * 「缓存」页发不出链接。这里守住同步规则，尤其是「非法 hash 不得让页面白屏」
 * ——用户手敲地址栏或从旧书签进来都可能带一个不存在的视图名。
 */
import assert from "node:assert/strict";
import test from "node:test";

import { installDom } from "./dom-stub.mjs";

const APP = new URL("../../pages/denia/app.js", import.meta.url);

let variant = 0;

async function loadApp({ hash = "" } = {}) {
  globalThis.__DENIA_NO_AUTOBOOT__ = true;
  const env = installDom({ hash });
  const url = new URL(APP);
  url.searchParams.set("v", String(++variant));
  const mod = await import(url.href);
  return { mod, env };
}

test("读取 hash：#/cache 能解析成视图名", async () => {
  const { mod, env } = await loadApp({ hash: "#/cache" });
  try {
    assert.equal(mod.viewFromHash(), "cache");
  } finally {
    env.teardown();
  }
});

test("读取 hash：容忍无斜杠写法 #cache", async () => {
  const { mod, env } = await loadApp({ hash: "#cache" });
  try {
    assert.equal(mod.viewFromHash(), "cache");
  } finally {
    env.teardown();
  }
});

test("读取 hash：空 / 未知 / 带多余路径都判为无值，交给调用方回退总览", async () => {
  for (const hash of ["", "#", "#/", "#/nope", "#/cache/123", "#//cache"]) {
    const { mod, env } = await loadApp({ hash });
    try {
      assert.equal(mod.viewFromHash(), null, `hash=${JSON.stringify(hash)} 应解析为 null`);
    } finally {
      env.teardown();
    }
  }
});

test("写回 hash：统一成 #/<view>", async () => {
  const { mod, env } = await loadApp();
  try {
    mod.writeHash("appearance");
    assert.equal(env.location.hash, "#/appearance");
  } finally {
    env.teardown();
  }
});

test("写回 hash：与当前相同则不重复赋值（否则 hashchange 会白白重挂视图）", async () => {
  const { mod, env } = await loadApp({ hash: "#/parse" });
  try {
    let reads = 0;
    Object.defineProperty(env.location, "hash", {
      get() {
        reads++;
        return "#/parse";
      },
      set() {
        throw new Error("相同 hash 不应被重新赋值");
      },
    });
    mod.writeHash("parse");
    assert.equal(reads, 1);
  } finally {
    env.teardown();
  }
});

test("每个视图都能通过 hash 往返", async () => {
  const { mod, env } = await loadApp();
  try {
    for (const name of Object.keys(mod.VIEWS)) {
      mod.writeHash(name);
      assert.equal(mod.viewFromHash(), name);
    }
  } finally {
    env.teardown();
  }
});
