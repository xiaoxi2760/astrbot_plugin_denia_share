/* 达妮娅工作台 · 页面控制器。
 *
 * 只做四件事：路由切换、真实接口动作、把状态交给 views、挂载/销毁复用视图。
 * 没有 mock 数据、没有假成功：接口失败会把后端 message 显示出来并回滚乐观更新。
 */

import {
  S,
  api,
  $,
  $$,
  esc,
  icon,
  names,
  toast,
  dialog,
  isDialogCurrent,
  close,
  loadCore,
  loadAppearance,
  initHostTheme,
  initDialog,
  applyValues,
  reloadRecords,
  savePrefs,
  applyThemeFromPrefs,
  updateThemeButton,
  toggleTheme,
  createContext,
  mountClassicView,
  mountClassicNodes,
  date,
  ago,
  contentType,
  layoutName,
} from "./core.js";
import { views, partials } from "./views.js";
import { loadSharedResources, applySharedResources } from "../shared-resources.js";
import { createCacheView } from "../classic/views/cache.js";
import { createConfigView } from "../classic/views/config.js";
import { createCredentialsPanel } from "../classic/credentials.js";
import { createCookieCloudPanel } from "../classic/cookiecloud.js";
import { createDiagnosticsPanel } from "../classic/diagnostics.js";
import {
  PREVIEW_OVERRIDES,
  OUTPUT_KEYS,
  buildPreviewOverrides,
  boolValue as toBool,
} from "./api.js";

const route = () => {
  const key = location.hash.replace(/^#\/?/, "").split("?")[0];
  return names[key] ? key : "workbench";
};

let current = null;
let viewGen = 0;
let previewTimer = 0;
let previewTicket = 0;
let homeTicket = 0;
let actionTicket = 0;
const panelState = new Map();

/* ---------------- 路由与挂载 ---------------- */

function disposeCurrent() {
  if (!current) return;
  if (typeof current.destroy === "function") {
    try { current.destroy(); } catch (error) { console.warn("[denia] 视图销毁失败", error); }
  }
  if (current.instance && typeof current.instance.unmount === "function") {
    try { current.instance.unmount(); } catch (error) { console.warn("[denia] 视图卸载失败", error); }
  }
  current = null;
}

function navState() {
  $$("[data-nav]").forEach((anchor) => {
    if (anchor.dataset.nav === S.view) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  });
  const badge = $("#nav-badge");
  if (badge) {
    badge.hidden = S.badge <= 0;
    badge.textContent = S.badge > 999 ? "999+" : String(S.badge);
  }
  updateHead();
}

function updateHead() {
  const node = $("#head-status");
  if (node) {
    node.hidden = !S.head;
    node.textContent = S.head || "";
    node.className = `status ${S.headKind || ""}`.trim();
  }
  const version = $("[data-version]");
  if (version) version.textContent = S.version ? `v${S.version}` : "";
}

function render() {
  const next = route();
  const changed = next !== S.view;
  for (const panel of $$("#main details[data-panel]")) {
    panelState.set(`${S.view}:${panel.dataset.panel}`, panel.open);
  }
  disposeCurrent();
  clearTimeout(previewTimer);
  previewTicket += 1;
  homeTicket += 1;
  viewGen += 1;
  const gen = viewGen;
  S.sampleBusy = false;
  S.homeSampleBusy = false;
  if (changed) {
    S.head = "";
    S.headKind = "";
  }
  S.view = next;
  $("#main").dataset.view = S.view;
  $("#main").innerHTML = views[S.view]();
  for (const panel of $$("#main details[data-panel]")) {
    const key = `${S.view}:${panel.dataset.panel}`;
    if (panelState.has(key)) panel.open = panelState.get(key);
  }
  document.title = `${names[S.view]} · 达妮娅的泡泡工坊`;
  navState();
  mountView(gen).catch((error) => mountError(gen, error));
}

function mountError(gen, error) {
  if (gen !== viewGen) return;
  console.error("[denia] 视图加载失败", error);
  const message = error?.message || String(error);
  $("#main").innerHTML = `<div class="empty-state"><h2>页面加载失败</h2><p>${esc(message)}</p><p>请确认插件后端可用；也可以打开经典界面继续操作。</p><button class="button" data-action="retry-boot">重新加载</button></div>`;
  toast(`页面加载失败：${message}`);
}

async function mountView(gen) {
  if (S.view === "workbench") return mountWorkbench(gen);
  if (S.view === "library") return mountLibrary(gen);
  if (S.view === "studio") return mountStudio(gen);
  if (S.view === "connections") return mountConnections(gen);
  if (S.view === "settings") return mountSettings(gen);
}

/* ---------------- 工作台 ---------------- */

async function mountWorkbench(gen) {
  hydrateThumbs(gen);
  if (!S.busy && !S.result && !S.screenshot && !S.error &&
      (!S.homeSample || S.homeSample.theme !== document.documentElement.dataset.theme) &&
      !S.homeSampleError && !S.homeSampleBusy) {
    await loadHomeSample(gen);
  }
}

async function loadHomeSample(gen) {
  const ticket = ++homeTicket;
  S.homeSampleBusy = true;
  S.homeSampleError = "";
  if (S.view === "workbench" && $("#workbench-preview")) {
    $("#workbench-preview").innerHTML = partials.workbenchPreview();
  }
  try {
    const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    const payload = await api.post("preview/sample", { preview: { theme } });
    if (ticket !== homeTicket) return;
    S.homeSample = payload;
  } catch (error) {
    if (ticket !== homeTicket) return;
    S.homeSample = null;
    S.homeSampleError = error?.message || String(error);
  } finally {
    if (ticket === homeTicket) {
      S.homeSampleBusy = false;
      if (gen === viewGen && S.view === "workbench") {
        updateWorkbenchPreview();
      }
    }
  }
}

function updateWorkbenchPreview() {
  const host = $("#workbench-preview");
  if (host) host.innerHTML = partials.workbenchPreview();
  const zoom = $('[data-action="preview-card"][data-preview="home"]');
  if (zoom) zoom.disabled = !(S.screenshot?.data_url || S.result?.card?.data_url || S.homeSample?.data_url);
}

async function hydrateThumbs(gen) {
  for (const node of $$("[data-thumb-id]")) {
    const id = node.dataset.thumbId;
    if (!id || S.thumbs[id] || S.thumbPending.has(id)) continue;
    S.thumbPending.add(id);
    try {
      const payload = await api.get("cache/thumbnail", { id });
      if (gen !== viewGen || !payload?.data_url) continue;
      S.thumbs[id] = payload.data_url;
      node.classList.add("has-image");
      node.innerHTML = `<img src="${esc(payload.data_url)}" alt="">`;
    } catch {
      node.classList.add("is-empty");
    } finally {
      S.thumbPending.delete(id);
    }
  }
}

async function runParse() {
  const input = $("#share-input");
  if (input) S.url = input.value;
  const url = String(S.url || "").trim();
  if (!url) {
    toast("先粘贴一个链接或分享口令");
    input?.focus();
    return;
  }
  if (S.busy) return;
  const mode = S.parseMode;
  const ticket = ++actionTicket;
  S.busy = true;
  S.error = "";
  S.result = null;
  S.screenshot = null;
  render();
  try {
    if (mode === "screenshot") {
      const payload = await api.post("screenshot", { url });
      if (ticket !== actionTicket) return;
      if (!payload?.data_url) throw new Error("服务器没有返回截图图片");
      S.screenshot = payload;
      toast("网页截图完成");
    } else {
      const payload = await api.post("parse", { url });
      if (ticket !== actionTicket) return;
      S.result = payload;
      S.homeSample = null;
      try {
        await reloadRecords(6);
      } catch (error) {
        console.warn("[denia] 解析成功，但刷新最近记录失败", error);
      }
      toast("解析完成，已写入分享记录");
    }
  } catch (error) {
    if (ticket !== actionTicket) return;
    S.result = null;
    S.screenshot = null;
    S.error = `${mode === "screenshot" ? "截图" : "解析"}失败：${error?.message || error}`;
  } finally {
    if (ticket === actionTicket) {
      S.busy = false;
      if (S.view === "workbench") render();
    }
  }
}

async function rerender() {
  const cacheKey = S.result?.cache_key;
  if (!cacheKey) return;
  try {
    const payload = await api.post("render", { cache_key: cacheKey, preview: {} });
    S.result = { ...S.result, ...payload, record_id: S.result.record_id };
    S.error = "";
    render();
    toast("已按当前配置重新渲染卡片");
  } catch (error) {
    toast(`重新渲染失败：${error?.message || error}`);
  }
}

async function toggleOutput(key) {
  const configKey = OUTPUT_KEYS[key];
  if (!configKey || S.outputSaving) return;
  S.outputSaving = true;
  const previous = toBool(S.values[configKey]);
  const next = !previous;
  S.values[configKey] = next;
  S.outputs[key] = next;
  if (S.view === "workbench") render();
  try {
    const result = await api.post("config", { values: { [configKey]: next } });
    if (Array.isArray(result.errors) && result.errors.length) {
      // The backend may save configuration but fail to apply it at runtime.
      // Reflect the confirmed values before displaying that partial failure.
      applyValues(result.values);
      throw new Error(result.errors.join("；"));
    }
    applyValues(result.values);
    toast(`${key === "text" ? "文本" : key === "card" ? "分享卡片" : "富媒体"}已${next ? "开启" : "关闭"}`);
  } catch (error) {
    // Read back after an ambiguous failure; a lost response does not prove the
    // write failed. Keep the previous value only if readback also fails.
    try { applyValues((await api.get("config")).values); }
    catch { S.values[configKey] = previous; S.outputs[key] = previous; }
    toast(`输出开关保存失败：${error?.message || error}`);
  } finally {
    S.outputSaving = false;
    if (S.view === "workbench") render();
  }
}

/* ---------------- 分享记录 ---------------- */

async function mountLibrary(gen) {
  const holder = $("#library-classic");
  if (!holder) return;
  const ctx = createContext();
  const { instance } = await mountClassicView(holder, (context) => createCacheView(context, { initialLimit: 10 }), ctx);
  if (gen !== viewGen) {
    instance.unmount?.();
    return;
  }
  current = { instance };
  const summary = $("#library-summary");
  if (summary && S.badge >= 0) summary.textContent = `共 ${S.badge} 条记录`;
}

/* ---------------- 卡片工坊 ---------------- */

function cardValue(key) {
  return Object.prototype.hasOwnProperty.call(S.cardDraft, key) ? S.cardDraft[key] : S.values[key];
}

function currentCardValues() {
  const values = {};
  for (const key of Object.keys(PREVIEW_OVERRIDES)) values[key] = cardValue(key);
  return values;
}

function normalizeCardValue(key, value) {
  if (["RENDER_WIDTH", "RENDER_DESC_MAX_LINES"].includes(key)) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.round(number) : undefined;
  }
  if (key === "RENDER_WATERMARK") return String(value ?? "").slice(0, 12);
  if (["RENDER_ACCENT_COLOR", "RENDER_GRADIENT_TOP", "RENDER_GRADIENT_BOTTOM"].includes(key)) {
    return String(value ?? "").trim().toUpperCase();
  }
  if (key === "RENDER_FONT_PATH") return String(value ?? "").trim();
  if (key === "RENDER_THEME" || key === "RENDER_LAYOUT") return String(value || "");
  return toBool(value);
}

function setCard(key, value) {
  const normalized = normalizeCardValue(key, value);
  if (normalized === undefined) return;
  const base = normalizeCardValue(key, S.values[key]);
  if (normalized === base) delete S.cardDraft[key];
  else S.cardDraft[key] = normalized;
  syncStudioDirty();
}

function syncStudioDirty() {
  const count = Object.keys(S.cardDraft).length;
  const button = $(".studio-save");
  if (button) {
    button.disabled = count === 0;
    button.innerHTML = `${count ? `保存 ${count} 项设计` : "设计已保存"} ${icon("check")}`;
  }
  const output = $("#width-output");
  if (output) output.textContent = `${esc(cardValue("RENDER_WIDTH"))} px`;
}

function schedulePreview(delay = 350) {
  if (S.view !== "studio") return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshStudioPreview, delay);
}

function updateStudioPreviewDom() {
  const host = $("#studio-preview");
  if (host) host.innerHTML = partials.studioPreview();
  const width = $("#board-width");
  if (width) width.textContent = String(S.sample?.width || cardValue("RENDER_WIDTH") || S.render.width);
  const layout = $("#atelier-layout");
  if (layout) layout.textContent = layoutName(S.sample?.layout || cardValue("RENDER_LAYOUT"));
  const exportButton = $('[data-action="export"]');
  if (exportButton) exportButton.disabled = S.sampleBusy;
}

async function refreshStudioPreview() {
  if (S.view !== "studio") return;
  const ticket = ++previewTicket;
  S.sampleBusy = true;
  S.sampleError = "";
  updateStudioPreviewDom();
  try {
    const payload = await api.post("preview/sample", { preview: buildPreviewOverrides(currentCardValues()) });
    if (ticket !== previewTicket) return;
    S.sample = payload;
  } catch (error) {
    if (ticket !== previewTicket) return;
    S.sample = null;
    S.sampleError = error?.message || String(error);
  } finally {
    if (ticket === previewTicket) {
      S.sampleBusy = false;
      updateStudioPreviewDom();
    }
  }
}

async function mountStudio(gen) {
  if (gen !== viewGen) return;
  schedulePreview(0);
}

async function saveStyle() {
  const values = { ...S.cardDraft };
  if (!Object.keys(values).length) {
    toast("还没有需要保存的设计修改");
    return;
  }
  const button = $(".studio-save");
  if (button) button.disabled = true;
  try {
    const result = await api.post("config", { values });
    if (Array.isArray(result.errors) && result.errors.length) {
      toast(`设计未保存：${result.errors.join("；")}`);
      return;
    }
    applyValues(result.values);
    S.cardDraft = {};
    toast(`已保存 ${result.changed?.length || 0} 项卡片设计`);
    render();
  } catch (error) {
    toast(`设计保存失败：${error?.message || error}`);
  } finally {
    if (button?.isConnected) button.disabled = Object.keys(S.cardDraft).length === 0;
  }
}

function downloadDataUrl(dataUrl, filename) {
  const match = /^data:([^;,]+)(;base64)?,(.*)$/s.exec(String(dataUrl || ""));
  if (!match) throw new Error("服务器没有返回可下载的图片数据");
  const mime = match[1];
  const binary = match[2] ? atob(match[3]) : decodeURIComponent(match[3]);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "denia-card.png";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportPreview() {
  if (S.sampleBusy) return;
  S.sampleBusy = true;
  updateStudioPreviewDom();
  try {
    const payload = await api.post("preview/sample", { preview: buildPreviewOverrides(currentCardValues()) });
    if (!payload?.data_url) throw new Error("服务器没有返回卡片图片");
    const ext = /^data:image\/(\w+)/.exec(payload.data_url)?.[1] || "png";
    const layout = cardValue("RENDER_LAYOUT") || payload.layout || "card";
    downloadDataUrl(payload.data_url, `denia-card-${layout}.${ext === "jpeg" ? "jpg" : ext}`);
    S.sample = payload;
    toast("已导出服务器渲染的卡片原图");
  } catch (error) {
    toast(`导出失败：${error?.message || error}`);
  } finally {
    S.sampleBusy = false;
    updateStudioPreviewDom();
  }
}

/* ---------------- 平台与账号 ---------------- */

function refreshConnectionPartials() {
  const rows = $("#platform-rows");
  if (rows) rows.innerHTML = partials.platformRows();
  const parsers = $("#custom-parsers-host");
  if (parsers) parsers.innerHTML = partials.customParsers();
  const cookiecloud = $("#cookiecloud-state");
  if (cookiecloud) {
    cookiecloud.textContent = S.cookiecloud?.unavailable ? "状态不可用" : S.cookiecloud?.configured ? "已配置" : "待配置";
    cookiecloud.className = `status ${S.cookiecloud?.configured ? "ok" : "unknown"}`;
  }
  const filters = $$('[data-action="platform-filter"]');
  filters.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.key === S.platformFilter)));
  const summary = $(".intro .library-summary");
  if (summary && S.ov) summary.textContent = `${S.ov.enabled_count || 0} / ${S.ov.platforms?.length || 0} 个平台已启用`;
}

async function loadConnectionsData() {
  const [credentials, custom, cookiecloud] = await Promise.allSettled([
    api.get("credentials"),
    api.get("custom_parsers"),
    api.get("cookiecloud"),
  ]);
  if (credentials.status === "fulfilled" && Array.isArray(credentials.value?.platforms)) {
    S.credentials = credentials.value.platforms;
  }
  if (custom.status === "fulfilled") S.custom = custom.value;
  else S.custom = { entries: [], errors: [{ file: "custom_parsers", reason: custom.reason?.message || "读取失败" }], dir: "", api_version: "", max_file_kb: "" };
  if (cookiecloud.status === "fulfilled") S.cookiecloud = cookiecloud.value;
  else S.cookiecloud = { unavailable: true };
}

async function mountConnections(gen) {
  const credentialsHost = $("#connections-credentials");
  const extraHost = $("#connections-extra");
  if (!credentialsHost || !extraHost) return;

  const ctx = createContext();
  const credentials = createCredentialsPanel(ctx, {
    collapsibleTips: true,
    onChange: (list) => {
      S.credentials = list;
      refreshConnectionPartials();
    },
  });
  const cookiecloud = createCookieCloudPanel(ctx, async () => {
    await credentials.load();
    S.credentials = credentials.platforms();
    refreshConnectionPartials();
  });
  const diagnostics = createDiagnosticsPanel(ctx);

  await loadConnectionsData();
  await credentials.load().catch(() => false);
  await cookiecloud.load().catch(() => false);
  if (gen !== viewGen) {
    credentials.cancelLogin();
    return;
  }
  S.credentials = credentials.platforms();

  await mountClassicNodes(credentialsHost, [credentials.node]);
  await mountClassicNodes(extraHost, [cookiecloud.node, diagnostics.node]);
  diagnostics.load().catch(() => {});
  refreshConnectionPartials();
  current = {
    destroy() {
      credentials.cancelLogin();
    },
  };
}

async function refreshPlatforms() {
  try {
    const [overview, credentials, cookiecloud, custom] = await Promise.all([
      api.get("overview"),
      api.get("credentials"),
      api.get("cookiecloud").catch(() => null),
      api.get("custom_parsers").catch(() => null),
    ]);
    S.ov = overview;
    if (Array.isArray(credentials?.platforms)) S.credentials = credentials.platforms;
    if (cookiecloud) S.cookiecloud = cookiecloud;
    if (custom) S.custom = custom;
    refreshConnectionPartials();
    toast("平台状态已刷新");
  } catch (error) {
    toast(`刷新失败：${error?.message || error}`);
  }
}

async function togglePlatform(button) {
  const name = button.dataset.key;
  const enabled = button.getAttribute("aria-checked") !== "true";
  button.disabled = true;
  try {
    const result = await api.post("platforms", { name, enabled });
    if (Array.isArray(result.errors) && result.errors.length) throw new Error(result.errors.join("；"));
    const overview = await api.get("overview");
    S.ov = overview;
    await loadConnectionsData();
    refreshConnectionPartials();
    toast(`${name} 已${enabled ? "启用" : "禁用"}`);
  } catch (error) {
    toast(`平台开关未保存：${error?.message || error}`);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function toggleCustomParser(button) {
  const key = button.dataset.key;
  const enabled = button.getAttribute("aria-checked") !== "true";
  button.disabled = true;
  try {
    const result = await api.post("platforms", { name: key, enabled });
    if (Array.isArray(result.errors) && result.errors.length) throw new Error(result.errors.join("；"));
    S.custom = await api.get("custom_parsers");
    refreshConnectionPartials();
    toast(`解析器已${enabled ? "启用" : "禁用"}`);
  } catch (error) {
    toast(`解析器开关未保存：${error?.message || error}`);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function reloadCustomParsers(button) {
  button.disabled = true;
  try {
    S.custom = await api.post("custom_parsers/reload", {});
    refreshConnectionPartials();
    toast(`已重新加载：成功 ${S.custom.entries?.length || 0} 个，失败 ${S.custom.errors?.length || 0} 个`);
  } catch (error) {
    toast(`重新加载失败：${error?.message || error}`);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

/* ---------------- 设置 ---------------- */

async function mountSettings(gen) {
  const holder = $("#settings-classic");
  if (!holder) return;
  const { instance } = await mountClassicView(holder,
    (context) => createConfigView(context, { showAccountPanels: false, collapsedByDefault: false }), createContext());
  if (gen !== viewGen) {
    instance.unmount?.();
    return;
  }
  current = { instance };
}

/* ---------------- 记录详情 / 诊断 ---------------- */

async function openRecord(id) {
  const record = S.records.find((item) => item.id === id);
  if (!record) {
    toast("这条记录已不在当前列表中，请到分享记录页查看");
    return;
  }
  const body = `<div class="record-detail">
    <div class="record-preview" id="record-preview">${record.card_exists ? '<span class="spinner"></span> 正在读取服务器卡片…' : '<span class="muted">卡片文件已被清理</span>'}</div>
    <dl class="info-list">
      <dt>平台</dt><dd>${esc(record.platform_display || record.platform)} · ${esc(contentType(record.content_type))}</dd>
      <dt>作者</dt><dd>${esc(record.author || "—")}</dd>
      <dt>解析时间</dt><dd>${esc(date(record.created_at))} · ${esc(ago(record.created_at))}</dd>
      <dt>来源</dt><dd>${esc({ auto: "链接自动", command: "命令", webui: "网页手动" }[record.via] || record.via || "—")}</dd>
      <dt>链接</dt><dd>${esc(record.url || "—")}</dd>
      <dt>媒体</dt><dd>${esc(record.media_count ?? (record.media_files || []).length)} 个文件，${esc(record.media_existing || 0)} 个源文件可下载</dd>
    </dl>
    ${record.detail?.text ? `<p class="result-text">${esc(record.detail.text)}</p>` : ""}
  </div>`;
  const actions = `<button class="button" data-action="close">关闭</button><button class="button" data-action="download-record" data-id="${esc(id)}" ${record.card_exists ? "" : "disabled"}>${icon("download")}下载卡片原图</button><button class="primary" data-action="download-source" data-id="${esc(id)}" ${record.media_existing > 0 ? "" : "disabled"}>${icon("download")}${record.media_existing > 1 ? "下载源文件 ZIP" : "下载源文件"}</button>`;
  const ticket = dialog("分享详情", body, actions);
  if (!record.card_exists) return;
  try {
    const thumb = await api.get("cache/thumbnail", { id });
    if (!isDialogCurrent(ticket)) return;
    const host = $("#record-preview");
    if (host && thumb?.data_url) host.innerHTML = `<img src="${esc(thumb.data_url)}" alt="服务器卡片缩略图">`;
  } catch (error) {
    if (!isDialogCurrent(ticket)) return;
    const host = $("#record-preview");
    if (host) host.innerHTML = `<span class="muted">缩略图读取失败：${esc(error?.message || error)}</span>`;
  }
}

async function downloadRecord(id) {
  const record = S.records.find((item) => item.id === id);
  const name = (record?.title || "denia-card").slice(0, 40);
  try {
    await api.download("cache/download", { id }, `${name}.png`);
    toast("已开始下载卡片原图");
  } catch (error) {
    toast(`下载失败：${error?.message || error}`);
  }
}

async function downloadSources(id) {
  const button = $(`[data-action="download-source"][data-id="${CSS.escape(id)}"]`);
  if (button) button.disabled = true;
  try {
    const info = await api.get("cache/media", { id });
    if (info.missing_count) toast(`${info.missing_count} 个源文件已清理或不可用，将下载其余 ${info.file_count} 个`);
    await api.download("cache/media/download", { id, archive: info.archive ? 1 : undefined }, info.filename);
    toast(info.archive ? "已开始下载源文件 ZIP" : "已开始下载源文件");
  } catch (error) {
    toast(`源文件下载失败：${error?.message || error}`);
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

async function openDiagnostics() {
  const ticket = dialog("解析诊断", '<p class="dialog-copy"><span class="spinner"></span> 正在读取最近的处理记录…</p>', '<button class="button" data-action="close">关闭</button>');
  try {
    const payload = await api.get("diagnostics");
    if (!isDialogCurrent(ticket)) return;
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const states = { running: "进行中", ok: "成功", skipped: "跳过", error: "失败", unknown: "待确认" };
    const body = items.length
      ? items.slice(0, 50).map((item) => `<details class="diag-item"><summary><strong>${esc(item.platform_display || item.platform || "未知平台")} · ${esc(states[item.status] || item.status)}</strong><span>${esc(item.via === "chat" ? "聊天" : "网页")} · ${esc(item.elapsed_ms ?? "—")} ms · ${esc(date(item.started_at))}</span></summary>${(item.stages || []).map((stage) => `<p>${esc(stage.label || stage.stage || "")}：${esc(states[stage.state] || stage.state || "")}${stage.message ? ` · ${esc(stage.message)}` : ""}</p>`).join("")}<button class="quiet-link" data-action="diagnostic-export" data-id="${esc(item.id)}">${icon("download")}导出脱敏报告</button></details>`).join("")
      : '<p class="dialog-copy">还没有诊断记录。解析一次内容后再来看。</p>';
    dialog("解析诊断", `<div class="diag-list">${body}</div>`, '<button class="button" data-action="close">关闭</button>');
  } catch (error) {
    if (!isDialogCurrent(ticket)) return;
    dialog("解析诊断", `<p class="dialog-copy">读取失败：${esc(error?.message || error)}</p>`, '<button class="button" data-action="close">关闭</button>');
  }
}

async function exportDiagnostic(id) {
  try {
    const { report } = await api.post("diagnostics/export", { id });
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `denia-diagnostic-${id}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("已导出脱敏诊断报告");
  } catch (error) {
    toast(`诊断导出失败：${error?.message || error}`);
  }
}

/* ---------------- 搜索 ---------------- */

function search() {
  dialog("要找什么？", '<input class="command-search" id="command-search" placeholder="搜索页面或配置项，例如：音乐、代理、输出" autocomplete="off" aria-label="搜索页面或配置项"><div class="search-results" id="search-results"></div>');
  searchResults("");
  $("#command-search")?.focus();
}

function searchResults(query) {
  const needle = String(query || "").toLowerCase();
  const pages = Object.entries(names).map(([id, label]) => ({ label, view: id, group: "页面" }));
  const items = (S.config?.items || []).filter((item) => !item.secret).map((item) => ({
    label: item.label,
    view: "settings",
    group: item.group || "配置",
  }));
  const found = [...pages, ...items]
    .filter((entry) => `${entry.label} ${entry.group}`.toLowerCase().includes(needle))
    .slice(0, 10);
  const host = $("#search-results");
  if (!host) return;
  host.innerHTML = found.length
    ? found.map((entry) => `<button data-action="search-go" data-key="${entry.view}"><span>${esc(entry.label)}</span><small>${esc(entry.group)} ↗</small></button>`).join("")
    : '<p class="subtle-note">没找到匹配项，换个词试试。</p>';
}

/* ---------------- 动作分发 ---------------- */

const pendingButtons = new WeakSet();

async function action(button) {
  const type = button.dataset.action;
  const key = button.dataset.key;
  const id = button.dataset.id;
  switch (type) {
    case "preview-card": {
      const source = button.dataset.preview === "home"
        ? S.screenshot?.data_url || S.result?.card?.data_url || S.homeSample?.data_url
        : S.sample?.data_url;
      if (source) dialog("卡片预览", `<img class="enlarged-preview" src="${esc(source)}" alt="分享卡片大图">`,
        '<button class="button" data-action="close">关闭</button>');
      break;
    }
    case "parse-mode":
      if (S.busy) return;
      S.parseMode = key;
      S.error = "";
      if (key === "parse") S.screenshot = null;
      else S.result = null;
      render();
      break;
    case "parse":
      await runParse();
      break;
    case "paste":
      try {
        const text = await navigator.clipboard.readText();
        S.url = text;
        if ($("#share-input")) $("#share-input").value = text;
        $("#share-input")?.focus();
      } catch {
        toast("无法读取剪贴板，请使用 Ctrl / ⌘ V 粘贴");
      }
      break;
    case "copy":
      try {
        await navigator.clipboard.writeText(S.result?.url || S.url);
        toast("链接已复制");
      } catch {
        toast("复制不可用，请从输入框手动复制");
      }
      break;
    case "output":
      await toggleOutput(key);
      break;
    case "rerender":
      await rerender();
      break;
    case "retry-sample":
      S.homeSampleError = "";
      await loadHomeSample(viewGen);
      break;
    case "studio-preview":
      S.sampleError = "";
      await refreshStudioPreview();
      break;
    case "record":
      await openRecord(id);
      break;
    case "download-record":
      await downloadRecord(id);
      break;
    case "download-source":
      await downloadSources(id);
      break;
    case "diagnostics":
      await openDiagnostics();
      break;
    case "diagnostic-export":
      await exportDiagnostic(id);
      break;
    case "refresh-platforms":
      await refreshPlatforms();
      break;
    case "platform-filter":
      S.platformFilter = key;
      refreshConnectionPartials();
      break;
    case "platform-toggle":
      await togglePlatform(button);
      break;
    case "custom-parser-toggle":
      await toggleCustomParser(button);
      break;
    case "reload-parsers":
      await reloadCustomParsers(button);
      break;
    case "theme-override": {
      savePrefs({ theme_override: key }, { onError: (error) => toast(`主题保存失败：${error.message}`) });
      applyThemeFromPrefs();
      updateThemeButton();
      render();
      break;
    }
    case "pref-accent": {
      const next = S.prefs.accent === key ? "" : key;
      savePrefs({ accent: next }, { onError: (error) => toast(`主题色保存失败：${error.message}`) });
      render();
      break;
    }
    case "pref-radius":
      savePrefs({ radius: key }, { onError: (error) => toast(`圆角保存失败：${error.message}`) });
      render();
      break;
    case "pref-toggle": {
      const next = !Boolean(S.prefs[key]);
      savePrefs({ [key]: next }, { onError: (error) => toast(`外观偏好保存失败：${error.message}`) });
      render();
      break;
    }
    case "card-theme":
      setCard("RENDER_THEME", key);
      render();
      break;
    case "card-layout":
      setCard("RENDER_LAYOUT", key);
      render();
      break;
    case "card-color":
      setCard("RENDER_ACCENT_COLOR", key);
      render();
      break;
    case "card-color-reset":
      setCard(key, "");
      render();
      break;
    case "card-toggle": {
      setCard(key, !toBool(cardValue(key)));
      render();
      break;
    }
    case "board-theme": {
      S.boardDark = key === "dark";
      const board = $(".studio-board");
      if (board) board.classList.toggle("is-dark", S.boardDark);
      $$('[data-action="board-theme"]').forEach((node) => node.setAttribute("aria-pressed", String(node.dataset.key === key)));
      break;
    }
    case "save-style":
      await saveStyle();
      break;
    case "export":
      await exportPreview();
      break;
    case "search-go":
      close();
      if (names[key]) location.hash = `/${key}`;
      break;
    case "close":
      close();
      break;
    case "retry-boot":
      location.reload();
      break;
    default:
      break;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  event.preventDefault();
  if (pendingButtons.has(button)) return;
  pendingButtons.add(button);
  action(button)
    .catch((error) => toast(`操作没有完成：${error?.message || error}`))
    .finally(() => pendingButtons.delete(button));
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (target.id === "share-input") {
    S.url = target.value;
    return;
  }
  if (target.id === "command-search") {
    searchResults(target.value);
    return;
  }
  if (target.dataset.cardInput) {
    const key = target.dataset.cardInput;
    setCard(key, target.value);
    if (key === "RENDER_WIDTH") {
      const text = $("#width-output");
      if (text) text.textContent = `${target.value} px`;
      const board = $("#board-width");
      if (board) board.textContent = target.value;
    }
    if (["RENDER_ACCENT_COLOR", "RENDER_GRADIENT_TOP", "RENDER_GRADIENT_BOTTOM"].includes(key)) {
      const picker = $(`[data-color-for="${key}"]`);
      if (picker && /^#[0-9a-f]{6}$/i.test(target.value)) picker.value = target.value;
    }
    if (key !== "RENDER_FONT_PATH") schedulePreview();
    return;
  }
  if (target.dataset.colorFor) {
    const key = target.dataset.colorFor;
    setCard(key, target.value);
    const text = $(`[data-card-input="${key}"]`);
    if (text) text.value = target.value.toUpperCase();
    schedulePreview();
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.dataset.prefColor) {
    savePrefs({ accent: "custom", accent_custom: target.value.toUpperCase() }, {
      onError: (error) => toast(`主题色保存失败：${error.message}`),
    });
    render();
  }
});

document.addEventListener("submit", (event) => event.preventDefault());

window.addEventListener("hashchange", () => {
  render();
  window.scrollTo({ top: 0 });
});

window.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (!$("#dialog")?.open) search();
  }
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && S.view === "workbench" && !$("#dialog")?.open) {
    event.preventDefault();
    runParse();
  }
  if (["ArrowLeft", "ArrowRight"].includes(event.key) && event.target.matches('[role="tab"]')) {
    event.preventDefault();
    $('[data-action="parse-mode"]:not([aria-selected="true"])')?.click();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (Object.keys(S.cardDraft).length) {
    event.preventDefault();
    event.returnValue = "";
  }
});

/* ---------------- 启动 ---------------- */

function bindShell() {
  // Sample appearance follows the character's light/dark form. Keep actual
  // parse results and the card designer's explicitly selected theme intact.
  let observedTheme = document.documentElement.dataset.theme;
  const themeObserver = new MutationObserver(() => {
    const theme = document.documentElement.dataset.theme;
    if (theme === observedTheme) return;
    observedTheme = theme;
    homeTicket += 1;
    S.homeSample = null;
    S.homeSampleError = "";
    S.homeSampleBusy = false;
    if (S.booted && S.view === "workbench" && !S.busy && !S.result && !S.screenshot && !S.error) {
      loadHomeSample(viewGen);
    }
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const searchButton = $("#search-button");
  if (searchButton) {
    searchButton.innerHTML = icon("search");
    searchButton.addEventListener("click", search);
  }
  $("#theme-button")?.addEventListener("click", () => {
    toggleTheme();
    updateThemeButton();
    $$('[data-action="theme-override"]').forEach((node) => {
      node.setAttribute("aria-pressed", String(S.prefs.theme_override === node.dataset.key));
    });
  });
  window.addEventListener("denia:prefs-rollback", () => {
    updateThemeButton();
    if (["studio", "settings", "connections", "library"].includes(S.view)) render();
  });
  const classic = new URL(location.href);
  classic.searchParams.set("ui", "classic");
  $$("[data-open-classic]").forEach((anchor) => { anchor.href = classic.href; });
  $("#dialog")?.addEventListener("click", (event) => {
    if (event.target.id === "dialog") close();
  });
}

async function boot() {
  initDialog();
  initHostTheme();
  bindShell();
  try {
    await Promise.all([loadAppearance(), loadSharedResources(api)]);
    applySharedResources();
    await loadCore();
    const ctx = createContext();
    ctx.setConnection(true, "已连接插件接口");
    ctx.setBadge(S.library?.total || 0);
    render();
  } catch (error) {
    console.error("[denia] 工作台启动失败", error);
    $("#main").innerHTML = `<div class="empty-state"><h2>无法连接插件后端</h2><p>${esc(error?.message || error)}</p><p>请确认插件已启用、AstrBot 的页面 bridge 可用；也可以打开经典界面继续操作。</p><button class="button" data-action="retry-boot">重新加载</button></div>`;
    toast("无法连接插件后端，未使用任何示例数据");
  }
}

boot();
