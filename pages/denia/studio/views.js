/* 达妮娅工作台 · 视图模板（纯字符串，不含业务动作）。
 *
 * 新版的布局、角色双形态、泡泡/方块视觉都保留；所有内容都来自真实状态 S。
 * 没有任何演示 URL、假卡片或「示例解析完成」文案：无结果时只显示空态或
 * 服务端 sample_preview 返回的图片，并明确标注「样例 · 不创建记录」。
 */

import {
  S,
  esc,
  icon,
  intro,
  asset,
  ago,
  date,
  layoutName,
  outputNote,
  status,
  contentType,
  names,
} from "./core.js";
import { CARD_LAYOUTS, CARD_THEMES, boolValue } from "./api.js";
import { platformIconUrl } from "./platform-icons.js";

export const PLATFORM_COLORS = {
  bilibili: "#dc7e95",
  douyin: "#3c4745",
  kuaishou: "#d6864e",
  weibo: "#c37059",
  xiaohongshu: "#c76e6d",
  qqmusic: "#719a56",
  netease: "#bd725f",
  steam: "#6f8993",
  xiaoheihe: "#777f73",
  acfun: "#ba7665",
  pixiv: "#6f8fb5",
  github: "#5b6470",
  twitter: "#6b8ea8",
  nga: "#b08a5a",
};

const output = (key, label, glyph) => {
  const pressed = Boolean(S.outputs[key]);
  const blocked = key === "card" && !S.render.enabled;
  return `<button class="output-option${blocked ? " is-blocked" : ""}" data-action="output" data-key="${key}" aria-pressed="${pressed}" aria-label="${label}" ${S.outputSaving ? "disabled" : ""}><span class="option-icon">${icon(glyph)}${label}</span><span class="checkmark" aria-hidden="true">✓</span></button>`;
};

const tabs = [["parse", "链接解析"], ["screenshot", "网页截图"]];

function workbenchPreview() {
  if (S.screenshot?.data_url) {
    return `<figure class="server-preview"><img class="preview-shot" src="${esc(S.screenshot.data_url)}" alt="服务器返回的网页截图"></figure>`;
  }
  if (S.busy) return `<div class="preview-empty"><span class="spinner"></span><p>正在等待服务器返回结果…</p></div>`;
  if (S.error) {
    return `<div class="preview-empty preview-error">${icon("warning")}<p><strong>没有生成预览</strong></p><p>${esc(S.error)}</p></div>`;
  }
  if (S.result) {
    if (S.result.card?.data_url) {
      return `<figure class="server-preview"><img class="preview-shot" src="${esc(S.result.card.data_url)}" alt="卡片预览的分享卡片"></figure>`;
    }
    if (!S.render.enabled) {
      return `<div class="preview-empty">${icon("info")}<p><strong>卡片渲染总开关已关闭</strong></p><p>解析结果已返回，但服务端没有生成卡片图片。可在卡片工坊开启后再试。</p></div>`;
    }
    return `<div class="preview-empty preview-error">${icon("warning")}<p><strong>服务端没有返回卡片图</strong></p><p>内容已解析成功，但卡片渲染失败。请查看解析诊断或稍后重试。</p></div>`;
  }
  if (S.homeSample?.data_url) {
    return `<figure class="server-preview is-sample"><img class="preview-shot" src="${esc(S.homeSample.data_url)}" alt="样例卡片"></figure>`;
  }
  if (S.homeSampleBusy) return `<div class="preview-empty"><span class="spinner"></span><p>正在生成样例预览…</p></div>`;
  if (S.homeSampleError) {
    return `<div class="preview-empty preview-error">${icon("warning")}<p><strong>样例预览不可用</strong></p><p>${esc(S.homeSampleError)}</p><button class="quiet-link" data-action="retry-sample">重试</button></div>`;
  }
  return `<div class="preview-empty">${icon("card")}<p>解析后的卡片会放在这里。</p><p class="subtle-note">现在还没有解析结果，样例图也暂时不可用。</p></div>`;
}

function resultDetails() {
  if (S.busy) return `<div class="result-state"><span><span class="spinner"></span> ${S.parseMode === "screenshot" ? "正在截图…" : "正在解析并渲染卡片…"}</span></div>`;
  if (S.error) {
    return `<div class="result-state error"><span>${icon("warning")} ${esc(S.error)}</span><button class="quiet-link" data-action="diagnostics">查看诊断 ${icon("arrow")}</button></div>`;
  }
  if (S.screenshot?.data_url) {
    return `<div class="result-state"><span>${icon("check")} 截图完成 · ${esc(S.screenshot.url || S.url)}</span><span>${esc(S.screenshot.file || "")} · ${esc(S.screenshot.size_text || "")}</span></div>`;
  }
  if (!S.result) return "";
  const detail = S.result.detail || {};
  const warnings = Array.isArray(detail.warnings) ? detail.warnings : [];
  const info = [
    ["平台", `${S.result.platform_display || S.result.platform || "—"} · ${contentType(S.result.content_type)}`],
    ["作者", S.result.author || "—"],
    ["解析耗时", S.result.elapsed_ms != null ? `${S.result.elapsed_ms} ms` : "—"],
    ["媒体", `图片 ${detail.image_count ?? 0} · 视频 ${detail.video_count ?? 0} · 音频 ${detail.audio_count ?? 0}`],
    ["链接", S.result.url || "—"],
  ];
  if (detail.stats_line) info.push(["数据", detail.stats_line]);
  if (detail.duration) info.push(["时长", detail.duration]);
  return `<div class="result-state"><span>${icon("check")} 解析完成 · ${esc(S.result.title || "已获取内容")}</span><button class="quiet-link" data-action="copy">复制链接 ${icon("copy")}</button></div>
    ${warnings.length ? `<ul class="warn-list">${warnings.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
    <details class="content-details" data-panel="result-details"><summary>内容详情</summary>
      <dl class="result-grid">${info.map(([key, value]) => `<dt>${key}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
      ${detail.text ? `<p class="result-text">${esc(detail.text)}</p>` : ""}
    </details>`;
}

function recent(record) {
  const thumb = S.thumbs[record.id];
  const thumbNode = record.card_exists
    ? `<span class="recent-thumb${thumb ? " has-image" : ""}" data-thumb-id="${esc(record.id)}">${thumb ? `<img src="${esc(thumb)}" alt="">` : icon("image")}</span>`
    : `<span class="recent-thumb is-empty">${icon("image")}</span>`;
  const title = record.title || record.url || "(无标题)";
  return `<button class="recent-item" data-action="record" data-id="${esc(record.id)}">${thumbNode}<span class="recent-copy"><strong class="recent-title" title="${esc(title)}">${esc(title)}</strong><span class="recent-meta">${esc(record.platform_display || record.platform)} · ${ago(record.created_at)}</span></span></button>`;
}

export function workbench() {
  const ov = S.ov || { platforms: [], cache: {} };
  const enabled = ov.enabled_count || 0;
  return intro(
    "DENIA’S WORKSHOP",
    "好吧好吧，<span class=\"heading-tail\">链接交给我。</span>",
    "视频、图文或音乐，贴上链接就能解析。好了，再看看要分享什么吧。",
    `<div class="denia-presence" aria-hidden="true"><div class="form-orbit"></div><div class="character-window"><img class="form-light" src="${asset("denia-light-pose.webp")}" alt=""><img class="form-dark" src="${asset("denia-dark-pose.webp")}" alt=""></div><span class="form-particle p1"></span><span class="form-particle p2"></span><span class="form-particle p3"></span><div class="form-caption"><span class="light-copy">01 / BUBBLE</span><span class="dark-copy">02 / VIRTUAL MATTER</span><strong>DENIA</strong></div></div>`,
  ) + `
  <section class="workspace">
    <div class="compose">
      <div class="section-line">
        <div class="tabs" role="tablist" aria-label="工作模式">${tabs.map(([key, label]) => `<button role="tab" data-action="parse-mode" data-key="${key}" aria-selected="${S.parseMode === key}">${label}</button>`).join("")}</div>
        <button class="icon-button" data-action="paste" aria-label="从剪贴板粘贴">${icon("paste")}</button>
      </div>
      <label class="entry-label" for="share-input">${S.parseMode === "parse" ? "粘贴链接、BV 号或完整分享口令" : "输入需要截图的网页地址"}</label>
      <textarea id="share-input" class="link-input" placeholder="${S.parseMode === "parse" ? "链接、BV 号或完整分享口令，放在这里就好。" : "https://…"}" spellcheck="false">${esc(S.url)}</textarea>
      <div class="compose-actions">
        <span class="subtle-note">${S.parseMode === "parse" ? "解析后可查看卡片和内容详情，并保存到分享记录。" : "截图服务可在「设置」中配置。"}</span>
        <button class="primary" data-action="parse" ${S.busy ? "disabled" : ""}>${S.busy ? '<span class="spinner"></span> 处理中' : `${S.parseMode === "parse" ? "开始解析" : "生成截图"} ${icon("arrow")}`}</button>
      </div>
      ${resultDetails()}
      <div class="output-section">
        <div class="label-line"><span>发送什么</span><small>三种内容，自由组合</small></div>
        <div class="output-options">${output("text", "文本", "text")}${output("card", "分享卡片", "card")}${output("media", "富媒体", "media")}</div>
        <p class="output-note">${outputNote()}</p>
      </div>
    </div>
    <aside class="preview-surface preview-stage">
      <div class="preview-rail preview-rail-left">
        <div><strong>分享预览</strong><small>PREVIEW</small></div>
        <div class="preview-rail-note"><span class="sample-flag">${S.result ? "解析结果" : S.screenshot ? "网页截图" : "样例"}</span><small>${S.result ? esc(layoutName(S.result.card?.layout || S.render.layout)) : S.screenshot ? esc(S.screenshot.size_text || "") : "不创建记录"}</small></div>
      </div>
      <div class="preview-artboard" id="workbench-preview">${workbenchPreview()}</div>
      <div class="preview-rail preview-rail-right">
        <a href="#/studio" class="preview-editor-link">调整外观 ${icon("arrow")}</a>
        <div class="preview-rail-actions">
          ${S.result?.cache_key ? '<button class="quiet-link" data-action="rerender">重新渲染</button>' : ""}
          <button class="button small" data-action="preview-card" data-preview="home" ${S.screenshot?.data_url || S.result?.card?.data_url || S.homeSample?.data_url ? "" : "disabled"}>${icon("search")}放大</button>
          <small>${S.result?.card ? `${esc(S.result.card.width)} px` : '<span class="light-copy">浅色形态</span><span class="dark-copy">深色形态</span>'}</small>
        </div>
      </div>
    </aside>
  </section>
  <div class="workspace-foot">
    <span class="ok">${icon("check")}${esc(S.connection || "已连接插件接口")}</span>
    <span>${enabled} / ${ov.platforms?.length || 0} 平台已启用</span>
    <span>${S.library?.total ?? S.records.length} 条分享记录</span>
    <span>${esc(ov.cache?.size_text || "缓存统计不可用")}</span>
    <a class="right" href="#/settings">输出与投递设置 ↗</a>
  </div>
  <section class="recent-area">
    <div class="section-line"><h2>最近的分享</h2><a class="quiet-link" href="#/library">查看全部 ${icon("arrow")}</a></div>
    ${S.records.length ? `<div class="recent-grid">${S.records.slice(0, 6).map(recent).join("")}</div>` : `<div class="empty-state">${icon("link")}<h3>这里还空着</h3><p>解析过的内容会自动出现在这里，慢慢来。</p></div>`}
  </section>`;
}

export function library() {
  return intro(
    "SHARE ARCHIVE",
    "想找之前的？都在这里。",
    "找找之前的分享吧。可以筛选记录、查看卡片、下载原图，也可以删除记录。",
    `<span class="library-summary" id="library-summary">${S.library ? `共 ${S.library.total} 条记录` : "正在读取服务器记录…"}</span>`,
  ) + `<section class="outlet-panel"><div id="library-classic" class="classic-outlet" aria-live="polite"><p class="outlet-loading"><span class="spinner"></span> 正在载入分享记录…</p></div></section>`;
}

function cardValue(key) {
  return Object.prototype.hasOwnProperty.call(S.cardDraft, key) ? S.cardDraft[key] : S.values[key];
}

function themeButtons() {
  return CARD_THEMES.map((key) => `<button class="layout-choice" data-action="card-theme" data-key="${key}" aria-pressed="${cardValue("RENDER_THEME") === key}"><span class="layout-glyph ${key}"></span>${key === "dark" ? "深色卡片" : "浅色卡片"}</button>`).join("");
}

function layoutButtons() {
  return CARD_LAYOUTS.map((key) => `<button class="layout-choice" data-action="card-layout" data-key="${key}" aria-pressed="${cardValue("RENDER_LAYOUT") === key}"><span class="layout-glyph ${key}"></span>${layoutName(key)}</button>`).join("");
}

function colorField(label, key, hint) {
  const value = String(cardValue(key) ?? "");
  return `<div class="field-row"><div><strong>${label}</strong>${hint ? `<p>${esc(hint)}</p>` : ""}</div><div class="field-control color-field"><input type="text" class="command-search" data-card-input="${key}" value="${esc(value)}" placeholder="#RRGGBB（留空默认）" spellcheck="false" aria-label="${label}"><input type="color" data-color-for="${key}" value="${/^#[0-9a-f]{6}$/i.test(value) ? value : "#658c69"}" aria-label="${label} 拾色器"><button class="quiet-link" data-action="card-color-reset" data-key="${key}">默认</button></div></div>`;
}

function toggleRow(label, key, hint, disabled = false) {
  return `<div class="toggle-line"><span>${label}${hint ? `<small>${esc(hint)}</small>` : ""}</span><button role="switch" class="switch" data-action="card-toggle" data-key="${key}" aria-label="${label}" aria-checked="${boolValue(cardValue(key))}" ${disabled ? "disabled" : ""}></button></div>`;
}

function prefToggle(label, key, onText = "开启", offText = "关闭") {
  const value = key === "compact" ? S.compact : key === "animations" ? S.animations : false;
  return `<div class="toggle-line"><span>${label}</span><button role="switch" class="switch" data-action="pref-toggle" data-key="${key}" aria-label="${label}" aria-checked="${Boolean(value)}"></button><small class="toggle-state">${value ? onText : offText}</small></div>`;
}

function studioPreview() {
  if (S.sampleBusy && !S.sample?.data_url) return `<div class="preview-empty"><span class="spinner"></span><p>服务器正在渲染样例卡片…</p></div>`;
  if (S.sample?.data_url) {
    return `<figure class="server-preview is-sample"><img class="preview-shot" src="${esc(S.sample.data_url)}" alt="服务器实际渲染的卡片样例"${S.sampleBusy ? ' style="opacity:.55"' : ""}></figure>`;
  }
  if (S.sampleError) return `<div class="preview-empty preview-error">${icon("warning")}<p><strong>预览渲染失败</strong></p><p>${esc(S.sampleError)}</p><button class="quiet-link" data-action="studio-preview">重试</button></div>`;
  return `<div class="preview-empty">${icon("card")}<p>调整设计后，这里会自动重新渲染样例。</p></div>`;
}

export function studio() {
  const render = S.render;
  const immersive = cardValue("RENDER_LAYOUT") === "immersive";
  const draftCount = Object.keys(S.cardDraft).length;
  const accent = String(cardValue("RENDER_ACCENT_COLOR") ?? "");
  const swatches = ["#658c69", "#b67c62", "#7884a0", "#ae8092", "#3d4140"];
  return intro(
    "CARD ATELIER",
    "卡片的样子……再挑挑看？",
    "版式、配色和文字都可以调整。先看看样例，再决定要不要保存。",
    `<button class="button" data-action="export" ${S.sampleBusy ? "disabled" : ""}>${icon("download")}导出渲染图</button>`,
  ) + `
  <div class="studio-layout">
    <aside class="studio-controls">
      <div class="atelier-settings">
      <section class="control-section studio-common">
        <h3>卡片形态</h3>
        <div class="layout-choices">${themeButtons()}</div>
        <div class="layout-choices" style="margin-top:8px">${layoutButtons()}</div>
      </section>
      <section class="control-section studio-common">
        <h3>卡片强调色</h3>
        <div class="color-options">${swatches.map((value) => `<button class="color-dot" style="--swatch:${value}" data-action="card-color" data-key="${value}" aria-label="强调色 ${value}" aria-pressed="${accent.toUpperCase() === value.toUpperCase()}"></button>`).join("")}</div>
        <div class="field-control color-field" style="margin-top:10px"><input type="text" class="command-search" data-card-input="RENDER_ACCENT_COLOR" value="${esc(accent)}" placeholder="#RRGGBB（留空用平台色）" spellcheck="false" aria-label="自定义强调色"><input type="color" data-color-for="RENDER_ACCENT_COLOR" value="${/^#[0-9a-f]{6}$/i.test(accent) ? accent : "#658c69"}" aria-label="强调色拾色器"></div>
        <p class="subtle-note">留空时每个平台使用自己的品牌色。</p>
      </section>
      <section class="control-section studio-common">
        <h3>尺寸</h3>
        <div class="range-line"><label for="card-width">卡片宽度</label><output id="width-output">${esc(cardValue("RENDER_WIDTH") ?? render.width)} px</output></div>
        <input id="card-width" type="range" min="520" max="1080" step="10" value="${esc(cardValue("RENDER_WIDTH") ?? render.width)}" data-card-input="RENDER_WIDTH">

      </section>

      <details class="studio-detail" data-panel="studio-text">
        <summary>文字细节</summary>
        <div class="detail-body">
        <div class="field-row"><div><strong>卡片水印</strong><p>最长 12 字，留空不显示</p></div><div class="field-control"><input type="text" class="command-search" maxlength="12" data-card-input="RENDER_WATERMARK" value="${esc(cardValue("RENDER_WATERMARK") ?? "")}" aria-label="卡片水印"></div></div>
          <div class="field-row"><div><strong>正文行数</strong><p>0 表示按布局默认</p></div><div class="field-control"><input type="number" min="0" max="12" step="1" data-card-input="RENDER_DESC_MAX_LINES" value="${esc(cardValue("RENDER_DESC_MAX_LINES") ?? 4)}" aria-label="正文最大行数"></div></div>
          <div class="field-row"><div><strong>字体文件</strong><p>留空自动探测；保存后生效</p></div><div class="field-control"><input type="text" data-card-input="RENDER_FONT_PATH" value="${esc(cardValue("RENDER_FONT_PATH") ?? "")}" placeholder="C:\\Windows\\Fonts\\…" aria-label="自定义字体文件路径" spellcheck="false"></div></div>
        </div>
      </details>
      <details class="studio-detail" data-panel="studio-render">
        <summary>渲染细节</summary>
        <div class="detail-body">
          ${toggleRow("启用卡片渲染", "RENDER_ENABLED", "聊天里还需选中「分享卡片」")}
          ${toggleRow("作者头像", "RENDER_SHOW_AVATAR")}
          ${toggleRow("封面播放按钮", "RENDER_SHOW_PLAY_BUTTON")}
          ${toggleRow("封面完整展示", "RENDER_COVER_FULL_SIZE")}
          ${toggleRow("B站 AI 摘要", "RENDER_SHOW_AI_SUMMARY", "沉浸式布局不显示", immersive)}
          <p class="subtle-note">沉浸封面仅保留标题与播放信息，不显示 AI 摘要。</p>
        </div>
      </details>
      <details class="studio-detail" data-panel="studio-gradient">
        <summary>背景渐变</summary>
        <div class="detail-body">
          ${colorField("顶部色", "RENDER_GRADIENT_TOP")}
          ${colorField("底部色", "RENDER_GRADIENT_BOTTOM")}
        </div>
      </details>
      <details class="studio-detail" data-panel="studio-appearance">
        <summary>界面外观</summary>
        <div class="detail-body">
          <div class="filter-tabs" role="group" aria-label="主题模式">${[["follow", "跟随面板"], ["light", "浅色"], ["dark", "深色"]].map(([key, label]) => `<button data-action="theme-override" data-key="${key}" aria-pressed="${S.prefs.theme_override === key}">${label}</button>`).join("")}</div>
          <div class="color-options" style="margin-top:10px">${Object.entries(S.accentPresets).filter(([key]) => key).map(([key, preset]) => `<button class="color-dot" style="--swatch:${preset.a}" data-action="pref-accent" data-key="${esc(key)}" aria-label="主题色 ${esc(key)}" aria-pressed="${S.prefs.accent === key}"></button>`).join("")}<input type="color" class="color-dot color-picker" data-pref-color="1" value="${/^#[0-9a-f]{6}$/i.test(S.prefs.accent_custom || "") ? S.prefs.accent_custom : "#2f6fdd"}" aria-label="自定义主题色"></div>
          <div class="range-line" style="margin-top:12px"><span>圆角</span><span class="segment">${[["s", "小"], ["m", "标准"], ["l", "大"]].map(([key, label]) => `<button data-action="pref-radius" data-key="${key}" aria-pressed="${S.prefs.radius === key}">${label}</button>`).join("")}</span></div>
          ${prefToggle("紧凑布局", "compact", "紧凑", "宽松")}
          ${prefToggle("过渡动效", "animations")}
        </div>
      </details>
      </div>
      <div class="atelier-savebar"><button class="primary studio-save" data-action="save-style" ${draftCount ? "" : "disabled"}>${draftCount ? `保存 ${draftCount} 项设计` : "设计已保存"} ${icon("check")}</button></div>
    </aside>
    <section class="studio-board">
      <div class="preview-top atelier-toolbar"><strong>实时预览</strong><span class="sample-flag">样例</span><div class="atelier-toolbar-actions"><button class="quiet-link preview-zoom" data-action="preview-card">${icon("search")}放大</button><div class="segment" aria-label="画布背景"><button data-action="board-theme" data-key="light" aria-pressed="${!S.boardDark}">浅底</button><button data-action="board-theme" data-key="dark" aria-pressed="${Boolean(S.boardDark)}">深底</button></div></div></div>
      <div class="preview-artboard atelier-canvas" id="studio-preview">${studioPreview()}</div>
      <div class="atelier-meta"><span><b id="board-width">${esc(S.sample?.width || cardValue("RENDER_WIDTH") || render.width)}</b> px <i>·</i> <span id="atelier-layout">${esc(layoutName(S.sample?.layout || cardValue("RENDER_LAYOUT")))}</span></span><small>预览不创建记录</small></div>
    </section>
  </div>`;
}

function platformRows() {  const platforms = S.ov?.platforms || [];
  const rows = platforms.filter((platform) => {
    if (S.platformFilter === "enabled") return platform.enabled;
    if (S.platformFilter === "pending") return !platform.enabled;
    return true;
  });
  if (!rows.length) return `<div class="empty-state">${icon("shield")}<h3>没有符合条件的平台</h3><p>切换筛选，或重新加载插件后再试。</p></div>`;
  return rows.map((platform) => {
    const credential = S.credentials?.find((item) => item.id === platform.name);
    const [kind, label] = status(credential);
    const logo = platform.custom ? "" : platformIconUrl(platform.name);
    return `<div class="platform-row"><div class="platform-mark${logo ? " has-logo" : ""}" style="--brand-color:${PLATFORM_COLORS[platform.name] || "#748b72"}">${logo ? `<img class="platform-icon" src="${esc(logo)}" alt="" width="24" height="24">` : esc((platform.label || platform.name).slice(0, 1))}</div><div class="platform-copy"><strong>${esc(platform.label)}</strong><small>${platform.custom ? "自定义解析器" : "内置平台"} · ${credential ? esc(label) : "无独立登录项"}</small></div><span class="status ${kind}">${platform.enabled ? "已启用" : "已禁用"}</span><button class="switch" role="switch" data-action="platform-toggle" data-key="${esc(platform.name)}" data-enabled="${platform.enabled}" aria-label="启用 ${esc(platform.label)}" aria-checked="${platform.enabled}"></button></div>`;
  }).join("");
}

function customParsers() {
  const data = S.custom;
  if (!data) return `<p class="subtle-note">正在读取自定义解析器…</p>`;
  const entries = data.entries || [];
  const errors = data.errors || [];
  return `<div class="custom-parsers">
    <div class="label-line"><span>${entries.length ? `${entries.length} 个解析器` : "解析器"}</span><button class="quiet-link" data-action="reload-parsers">${icon("refresh")}重新加载</button></div>
    ${entries.length ? entries.map((entry) => `<div class="field-row"><div><strong>${esc(entry.label || entry.key)}</strong><p>${esc((entry.keywords || []).join(" · "))}</p></div><button class="switch" role="switch" data-action="custom-parser-toggle" data-key="${esc(entry.key)}" aria-checked="${entry.enabled}" aria-label="启用 ${esc(entry.label || entry.key)}"></button></div>`).join("") : `<p class="subtle-note">服务器目录里暂时没有自定义解析器。</p>`}
    ${errors.length ? `<div class="content-alert">${icon("warning")} ${errors.map((item) => `${esc(item.file)}：${esc(item.reason)}`).join("；")}</div>` : ""}
    <p class="subtle-note">目录：${esc(data.dir || "—")} · 单文件上限 ${esc(data.max_file_kb || "—")} KB</p>
  </div>`;
}

export function connections() {
  const ov = S.ov || { platforms: [], enabled_count: 0 };
  return intro(
    "PLATFORMS & ACCOUNTS",
    "平台与账号，都在这里。",
    "在这里管理平台开关、账号凭据、扫码登录和 CookieCloud 同步。",
    `<span class="library-summary">${ov.enabled_count || 0} / ${ov.platforms?.length || 0} 个平台已启用</span>`,
  ) + `
  <div class="connections-layout">
    <section>
      <div class="toolbar">
        <div class="filter-tabs" aria-label="平台筛选">${[["all", "全部平台"], ["enabled", "已启用"], ["pending", "待启用"]].map(([key, label]) => `<button data-action="platform-filter" data-key="${key}" aria-pressed="${S.platformFilter === key}">${label}</button>`).join("")}</div>
        <div class="filter-tabs"><button data-action="diagnostics">解析诊断</button><button data-action="refresh-platforms">${icon("refresh")}刷新状态</button></div>
      </div>
      <div class="platform-list" id="platform-rows">${platformRows()}</div>
      <details class="collapse-block" data-panel="cookiecloud">
        <summary><strong>CookieCloud</strong><span class="status ${S.cookiecloud?.configured ? "ok" : "unknown"}" id="cookiecloud-state">${S.cookiecloud?.unavailable ? "状态不可用" : S.cookiecloud?.configured ? "已配置" : "待配置"}</span></summary>
        <div class="collapse-body">从自建服务同步选定平台登录态；连接信息在下方凭据区配置。</div>
      </details>
      <details class="collapse-block" data-panel="custom-parsers">
        <summary>自定义解析器</summary>
        <div class="collapse-body" id="custom-parsers-host">${customParsers()}</div>
      </details>
    </section>
    <aside class="connection-aside">
      <div id="connections-credentials" class="classic-outlet"><p class="outlet-loading"><span class="spinner"></span> 正在载入账号凭据…</p></div>
    </aside>
  </div>
  <details class="collapse-block connections-tools" data-panel="connection-tools"><summary>登录态同步与诊断工具</summary><div id="connections-extra" class="classic-outlet"></div></details>`;
}

export function settings() {
  return intro(
    "MAKE IT YOURS",
    "设置嘛，按你的习惯来。",
    "所有设置都在这里。改好后记得保存。",
    `<a class="button" href="#/connections">账号与平台 ${icon("arrow")}</a>`,
  ) + `<section class="outlet-panel settings-frame"><div id="settings-classic" class="classic-outlet" aria-live="polite"><p class="outlet-loading"><span class="spinner"></span> 正在载入完整设置…</p></div></section>`;
}

export const views = { workbench, library, studio, connections, settings };

/** 供控制器局部刷新（不重建已挂载的 classic 凭据面板）。 */
export const partials = { platformRows, customParsers, workbenchPreview, studioPreview };

export { names };
