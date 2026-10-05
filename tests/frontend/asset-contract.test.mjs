/**
 * 页面静态契约测试（对标 meme_manager 的 tests/test_frontend_assets.py）。
 *
 * 这类检查不需要跑页面，纯静态比对就能挡住几类很难在联调时发现的破事：
 * - index.html 引用了不存在的文件（AstrBot 只会把 pages/ 目录整个发出去，
 *   少一个文件在浏览器里就是白屏，控制台还得靠人去翻）；
 * - 左导航加了按钮但 VIEWS 里没有同名条目（点了没反应，且不报错）；
 * - VIEWS 加了视图但导航里没有入口（功能做完了没人点得到）。
 *
 * 其中 logo 逐字节比对是 index.html 里的注释明确承诺的自检项。
 */
import assert from "node:assert/strict";
import { readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import test from "node:test";

const PAGES = new URL("../../pages/denia/", import.meta.url);
const PLUGIN_ROOT = new URL("../../", import.meta.url);

/** 读一份文本资源。 */
const readText = (rel) => readFile(new URL(rel, PAGES), "utf8");

async function viewKeys() {
  globalThis.__DENIA_NO_AUTOBOOT__ = true;
  const { installDom } = await import("./dom-stub.mjs");
  const env = installDom();
  const url = new URL("app.js", PAGES);
  url.searchParams.set("v", "assets");
  const { VIEWS } = await import(url.href);
  env.teardown();
  return Object.keys(VIEWS);
}

test("index.html 引用的本地资源都真实存在", async () => {
  const html = await readText("index.html");
  const refs = [...html.matchAll(/(?:href|src)="(\.\/[^"]+)"/g)].map((m) => m[1]);
  assert.ok(refs.length >= 2, `应至少引用样式与入口脚本，实际 ${refs.length}`);
  for (const ref of refs) {
    const info = await stat(new URL(ref, PAGES)).catch(() => null);
    assert.ok(info?.isFile(), `index.html 引用了不存在的文件：${ref}`);
  }
});

test("左导航的 data-view 与 VIEWS 完全一致（不多不少）", async () => {
  const html = await readText("index.html");
  const navKeys = [...html.matchAll(/data-view="([^"]+)"/g)].map((m) => m[1]);
  const keys = await viewKeys();
  assert.deepEqual(
    [...navKeys].sort(),
    [...keys].sort(),
    "导航项与 VIEWS 不一致：加视图时两处都要改",
  );
});

test("每个视图都有独立模块文件，且没有孤儿 JS", async () => {
  const appSource = await readText("app.js");
  const imported = [...appSource.matchAll(/from "\.\/(views\/[^"]+)"/g)].map((m) => m[1]);
  assert.ok(imported.length >= 5, `应导入各视图模块，实际 ${imported.length}`);
  for (const rel of imported) {
    const info = await stat(new URL(rel, PAGES)).catch(() => null);
    assert.ok(info?.isFile(), `app.js 引用了不存在的视图模块：${rel}`);
  }

  const onDisk = (await readdir(new URL("views/", PAGES))).filter((f) => f.endsWith(".js"));
  const referenced = new Set(imported.map((p) => p.replace(/^views\//, "")));
  for (const file of onDisk) {
    assert.ok(referenced.has(file), `views/${file} 没有任何地方引用（孤儿模块）`);
  }
});

test("页面 logo 与插件根 logo 逐字节一致", async () => {
  // index.html 注释里承诺了这条：品牌位直接指本页目录的 logo.png，
  // 页面资源又只能取本页目录内的，所以这里是同一张图的两份副本。
  const pageLogo = await readFile(new URL("logo.png", PAGES));
  const rootLogo = await readFile(new URL("logo.png", PLUGIN_ROOT));
  const digest = (buf) => createHash("sha256").update(buf).digest("hex");
  assert.equal(digest(pageLogo), digest(rootLogo), "两份 logo.png 已不同步");
  assert.equal(digest(pageLogo), digest(rootLogo));
});

test("主样式表定义了主题变量入口（prefs.js 依赖的 data-* 钩子）", async () => {
  const css = await readText("style.css");
  for (const hook of ["data-theme", "data-radius", "data-compact", "data-motion"]) {
    assert.ok(css.includes(hook), `style.css 缺少 ${hook} 选择器，prefs.js 派生会失效`);
  }
});
