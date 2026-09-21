/* 解析页：手动输入链接 → 立刻看到卡片预览与字段明细；附带网页截图小工具。 */

import {
  h,
  clear,
  card,
  toast,
  withBusy,
  previewFrame,
  placeholder,
  emptyBox,
  spinner,
  fmtMs,
} from "../ui.js";

const THEMES = [
  { value: "", label: "跟随配置" },
  { value: "dark", label: "深色" },
  { value: "light", label: "浅色" },
];

const LAYOUTS = [
  { value: "", label: "跟随配置" },
  { value: "standard", label: "standard 标准" },
  { value: "magazine", label: "magazine 杂志" },
  { value: "immersive", label: "immersive 沉浸" },
  { value: "feed", label: "feed 信息流" },
];

export function createParseView(ctx) {
  let container = null;
  let defaults = { theme: "dark", layout: "standard" };
  let overrides = { theme: "", layout: "" };
  let last = null;
  let busy = false;

  const elements = {};

  async function loadDefaults() {
    try {
      const overview = await ctx.api.get("overview");
      defaults = {
        theme: overview.render.theme || "dark",
        layout: overview.render.layout || "standard",
      };
      ctx.setVersion(overview.version ? `v${overview.version}` : "");
    } catch (error) {
      console.warn("读取默认外观失败", error);
    }
  }

  function render() {
    clear(container);

    container.appendChild(renderInputCard());
    if (last) {
      container.appendChild(renderResult());
    } else {
      container.appendChild(
        card("解析结果", null, [emptyBox("还没有解析结果，输入一个链接试试")]),
      );
    }
    container.appendChild(renderShotCard());
  }

  /* ---------------- 输入区 ---------------- */

  function renderInputCard() {
    const input = h("input", {
      class: "input",
      id: "parse-url",
      type: "text",
      placeholder: "粘贴分享链接，例如 https://www.bilibili.com/video/BV...",
      spellcheck: "false",
      onKeydown: (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          runParse(elements.parseButton);
        }
      },
    });
    elements.urlInput = input;

    const themeSelect = h(
      "select",
      {
        class: "select",
        onChange: (event) => {
          overrides.theme = event.target.value;
        },
      },
      THEMES.map((item) =>
        h("option", { value: item.value, text: item.label, selected: item.value === overrides.theme ? true : null }),
      ),
    );
    const layoutSelect = h(
      "select",
      {
        class: "select",
        onChange: (event) => {
          overrides.layout = event.target.value;
        },
      },
      LAYOUTS.map((item) =>
        h("option", { value: item.value, text: item.label, selected: item.value === overrides.layout ? true : null }),
      ),
    );
    elements.themeSelect = themeSelect;
    elements.layoutSelect = layoutSelect;

    const parseButton = h(
      "button",
      { class: "btn primary", type: "button", onClick: () => runParse(parseButton) },
      "解析",
    );
    elements.parseButton = parseButton;

    return card(
      "解析链接",
      "与聊天里自动解析走同一条链路",
      [
        h("div", { class: "toolbar" }, [h("div", { class: "grow" }, [input]), parseButton]),
        h("p", {
          class: "card-note",
          style: { marginTop: "10px" },
          text:
            "视频不会在这里下载，只取标题/封面/统计等信息用于预览；" +
            "主题与布局只影响本次预览，不改动插件配置。",
        }),
        h("div", { class: "toolbar" }, [
          h("span", { class: "unit", text: "预览主题" }),
          themeSelect,
          h("span", { class: "unit", text: "预览布局" }),
          layoutSelect,
        ]),
      ],
      h("button", { class: "btn tiny", type: "button", onClick: () => { last = null; render(); } }, "清空结果"),
    );
  }

  function previewOverrides() {
    const payload = {};
    if (overrides.theme) payload.theme = overrides.theme;
    if (overrides.layout) payload.layout = overrides.layout;
    return payload;
  }

  async function runParse(button) {
    const url = (elements.urlInput.value || "").trim();
    if (!url) {
      toast("请先填写链接", "err");
      elements.urlInput.focus();
      return;
    }
    if (busy) return;
    busy = true;

    const restore = withBusy(button, true, "解析中…");
    clear(container);
    container.appendChild(renderInputCard());
    container.appendChild(
      card("解析结果", null, [
        h("div", { class: "empty" }, [spinner(), " 正在解析，视频/图集较大时会慢一些…"]),
      ]),
    );
    container.appendChild(renderShotCard());

    try {
      const payload = await ctx.api.post("parse", {
        url,
        preview: previewOverrides(),
      });
      last = payload;
      render();
      toast(`解析成功：${payload.platform_display} · ${payload.content_type}`, "ok");
    } catch (error) {
      last = null;
      render();
      toast(`解析失败：${error.message}`, "err");
    } finally {
      busy = false;
      restore();
    }
  }

  /* ---------------- 结果区 ---------------- */

  function renderResult() {
    const detail = last.detail || {};
    const cardInfo = last.card;

    const previewNode = cardInfo
      ? previewFrame(cardInfo.data_url, "预览生成失败")
      : placeholder("本次没有生成卡片（可能已在「卡片外观」里关闭渲染）");

    const left = card(
      "卡片预览",
      cardInfo ? `${cardInfo.theme === "dark" ? "深色" : "浅色"} · ${cardInfo.layout} · ${cardInfo.width}px` : null,
      [
        previewNode,
        h("div", { class: "toolbar", style: { marginTop: "10px" } }, [
          h(
            "button",
            {
              class: "btn",
              type: "button",
              disabled: !last.record_id,
              onClick: (event) => rerender(event.currentTarget),
            },
            "按当前外观重渲染",
          ),
          h(
            "button",
            {
              class: "btn",
              type: "button",
              disabled: !last.record_id,
              onClick: () => downloadCard(),
            },
            "下载卡片原图",
          ),
        ]),
      ],
    );

    const rows = [
      ["平台", last.platform_display],
      ["类型", last.content_type],
      ["标题", last.title || "—"],
      ["作者", last.author || "—"],
      ["链接", last.url || "—"],
      ["耗时", fmtMs(last.elapsed_ms)],
      ["图片", `${detail.image_count || 0} 张`],
      ["视频", `${detail.video_count || 0} 个`],
      ["音频", `${detail.audio_count || 0} 个`],
    ];
    if (detail.stats_line) rows.push(["统计", detail.stats_line]);
    if (detail.duration) rows.push(["时长", detail.duration]);
    if (detail.online) rows.push(["在线", detail.online]);
    if (detail.info) rows.push(["附注", detail.info]);

    const warnings = detail.warnings || [];

    const right = card("解析结果", null, [
      h(
        "dl",
        { class: "detail-grid" },
        rows.flatMap(([key, value]) => [
          h("dt", { text: key }),
          h("dd", { text: String(value) }),
        ]),
      ),
      last.text
        ? h("div", { style: { marginTop: "10px" } }, [
            h("p", { class: "card-note", text: "正文节选" }),
            h("p", { style: { fontSize: "12px", margin: "0" }, text: last.text }),
          ])
        : null,
      warnings.length
        ? h("div", { style: { marginTop: "10px" } }, [
            ...warnings.map((item) => h("p", { class: "card-note", text: item })),
          ])
        : null,
    ]);

    return h("div", { class: "two-col" }, [left, right]);
  }

  async function rerender(button) {
    if (!last) return;
    const restore = withBusy(button, true, "渲染中…");
    try {
      const payload = await ctx.api.post("render", {
        cache_key: last.cache_key,
        preview: previewOverrides(),
      });
      // render 接口是以 record=False 调的，返回的 record_id 是空串 —— 不能让它
      // 覆盖解析时保存的记录 id，否则「保存卡片 / 下载」两个按钮会永久变灰
      // （它们靠 record_id 判断可用性）。
      last = { ...last, ...payload, record_id: last.record_id || payload.record_id };
      render();
      toast("已按预览外观重新渲染", "ok");
    } catch (error) {
      toast(`渲染失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  async function downloadCard() {
    if (!last || !last.record_id) return;
    try {
      const title = (last.title || last.platform_display || "card").slice(0, 40);
      await ctx.api.download("cache/download", { id: last.record_id }, `${title}.png`);
      toast("已开始下载卡片", "ok");
    } catch (error) {
      toast(`下载失败：${error.message}`, "err");
    }
  }

  /* ---------------- 网页截图 ---------------- */

  function renderShotCard() {
    const input = h("input", {
      class: "input",
      type: "text",
      placeholder: "https://example.com",
      spellcheck: "false",
      onKeydown: (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          runShot(shotButton);
        }
      },
    });
    const shotButton = h(
      "button",
      { class: "btn", type: "button", onClick: () => runShot(shotButton) },
      "截图",
    );
    const frame = h("div", { class: "shot-frame" }, [
      placeholder("截图结果会显示在这里（thum 后端约需数秒）"),
    ]);

    elements.shotInput = input;
    elements.shotFrame = frame;

    return card("网页截图", "与聊天里的 /shot 同一后端", [
      h("div", { class: "toolbar" }, [h("div", { class: "grow" }, [input]), shotButton]),
      h("div", { style: { marginTop: "10px" } }, [frame]),
    ]);
  }

  async function runShot(button) {
    const url = (elements.shotInput.value || "").trim();
    if (!url) {
      toast("请先填写网址", "err");
      return;
    }
    const restore = withBusy(button, true, "截图中…");
    clear(elements.shotFrame);
    elements.shotFrame.appendChild(h("div", { class: "empty" }, [spinner(), " 正在截图…"]));
    try {
      const result = await ctx.api.post("screenshot", { url });
      clear(elements.shotFrame);
      if (result.data_url) {
        elements.shotFrame.appendChild(h("img", { src: result.data_url, alt: "网页截图" }));
        toast(`截图完成（${result.backend} · ${result.size_text}）`, "ok");
      } else {
        elements.shotFrame.appendChild(placeholder("截图已生成，但预览图过大，未在页面展示"));
      }
    } catch (error) {
      clear(elements.shotFrame);
      elements.shotFrame.appendChild(placeholder(`截图失败：${error.message}`));
      toast(`截图失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  return {
    async mount(node) {
      container = node;
      await loadDefaults();
      render();
    },
    async refresh() {
      await loadDefaults();
      render();
    },
    unmount() {},
  };
}
