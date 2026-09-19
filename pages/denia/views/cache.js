/* 缓存页：解析记录的管理台 —— 搜索、筛选、分页、查看卡片、删除、清理。 */

import {
  h,
  clear,
  card,
  toast,
  confirmDialog,
  openModal,
  withBusy,
  fmtTime,
  fmtBytes,
  fmtMs,
  placeholder,
  emptyBox,
  spinner,
  VIA_LABELS,
} from "../ui.js";

const VIA_OPTIONS = [
  { value: "", label: "全部来源" },
  { value: "auto", label: "链接自动" },
  { value: "command", label: "命令" },
  { value: "webui", label: "网页手动" },
];

const LIMIT_OPTIONS = [10, 20, 50, 100];

export function createCacheView(ctx) {
  let container = null;
  let data = null;
  const query = { keyword: "", platform: "", via: "", limit: 20, offset: 0 };
  let searchTimer = null;

  async function load(showBusy = true) {
    if (showBusy) ctx.setHead("加载中…");
    data = await ctx.api.get("cache", {
      keyword: query.keyword,
      platform: query.platform,
      via: query.via,
      limit: query.limit,
      offset: query.offset,
    });
    ctx.setBadge(data.total || 0);
    ctx.setHead(`${data.total || 0} 条记录`);
  }

  function render() {
    if (!container || !data) return;
    clear(container);
    container.appendChild(renderToolbar());
    container.appendChild(renderList());
    container.appendChild(renderFooterActions());
  }

  /* ---------------- 工具条 ---------------- */

  function renderToolbar() {
    const search = h("input", {
      class: "input",
      type: "search",
      value: query.keyword,
      placeholder: "搜索标题 / 作者 / 链接",
      onInput: (event) => {
        query.keyword = event.target.value;
        query.offset = 0;
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => reload(), 320);
      },
    });

    const platformSelect = h(
      "select",
      {
        class: "select",
        onChange: (event) => {
          query.platform = event.target.value;
          query.offset = 0;
          reload();
        },
      },
      [{ name: "", label: "全部平台" }, ...(data.platforms || [])].map((item) =>
        h("option", {
          value: item.name,
          text: item.label,
          selected: item.name === query.platform ? true : null,
        }),
      ),
    );

    const viaSelect = h(
      "select",
      {
        class: "select",
        onChange: (event) => {
          query.via = event.target.value;
          query.offset = 0;
          reload();
        },
      },
      VIA_OPTIONS.map((item) =>
        h("option", {
          value: item.value,
          text: item.label,
          selected: item.value === query.via ? true : null,
        }),
      ),
    );

    const limitSelect = h(
      "select",
      {
        class: "select",
        onChange: (event) => {
          query.limit = Number(event.target.value) || 20;
          query.offset = 0;
          reload();
        },
      },
      LIMIT_OPTIONS.map((value) =>
        h("option", {
          value: String(value),
          text: `每页 ${value} 条`,
          selected: value === query.limit ? true : null,
        }),
      ),
    );

    const cache = data.cache || {};
    const stats = data.stats || {};

    return card(
      "解析记录",
      `共 ${data.total} 条 · 记录文件 ${fmtBytes(stats.size_bytes || 0)} · 缓存文件 ${cache.files || 0} 个 / ${cache.size_text || fmtBytes(cache.size_bytes || 0)}`,
      [
        h("div", { class: "toolbar" }, [
          h("div", { class: "grow" }, [search]),
          h("div", { style: { width: "130px" } }, [platformSelect]),
          h("div", { style: { width: "120px" } }, [viaSelect]),
          h("div", { style: { width: "112px" } }, [limitSelect]),
        ]),
      ],
    );
  }

  /* ---------------- 列表 ---------------- */

  function renderList() {
    if (!data.items.length) {
      return card(
        "记录列表",
        null,
        [
          emptyBox(
            query.keyword || query.platform || query.via
              ? "没有符合条件的记录"
              : "还没有解析记录。在聊天里发一个链接，或在「解析」页手动解析一次就会出现在这里",
          ),
        ],
      );
    }

    const from = query.offset + 1;
    const to = query.offset + data.items.length;

    const rows = data.items.map((record) =>
      h("div", { class: "row" }, [
        h("div", { class: "row-main" }, [
          h("div", { class: "row-title", text: record.title || record.url || "(无标题)" }),
          h("div", {
            class: "row-sub",
            text: [
              record.platform_display || record.platform,
              record.content_type,
              VIA_LABELS[record.via] || record.via,
              fmtMs(record.elapsed_ms),
            ].join(" · "),
          }),
        ]),
        h("div", { class: "row-meta", text: fmtTime(record.created_at) }),
        h("div", { class: "row-actions" }, [
          h(
            "button",
            { class: "btn tiny", type: "button", onClick: () => openDetail(record) },
            "查看",
          ),
          h(
            "button",
            {
              class: "btn tiny",
              type: "button",
              disabled: !record.card_exists,
              onClick: () => download(record),
            },
            "下载",
          ),
          h(
            "button",
            {
              class: "btn tiny danger",
              type: "button",
              onClick: (event) => removeOne(record, event.currentTarget),
            },
            "删除",
          ),
        ]),
      ]),
    );

    const pager = h("div", { class: "pager" }, [
      h("span", { text: `第 ${from}–${to} 条，共 ${data.total} 条` }),
      h("div", { class: "toolbar" }, [
        h(
          "button",
          {
            class: "btn tiny",
            type: "button",
            disabled: query.offset <= 0,
            onClick: () => {
              query.offset = Math.max(0, query.offset - query.limit);
              reload();
            },
          },
          "上一页",
        ),
        h(
          "button",
          {
            class: "btn tiny",
            type: "button",
            disabled: to >= data.total,
            onClick: () => {
              query.offset = query.offset + query.limit;
              reload();
            },
          },
          "下一页",
        ),
      ]),
    ]);

    return card("记录列表", null, [h("div", { class: "list" }, rows), pager]);
  }

  /* ---------------- 底部维护操作 ---------------- */

  function renderFooterActions() {
    const ttl = data.cache && data.cache.ttl_hours;
    const cleanupButton = h(
      "button",
      { class: "btn", type: "button", onClick: (event) => cleanup(event.currentTarget) },
      ttl ? `清理超过 ${ttl} 小时的文件` : "清理过期文件",
    );
    const clearRecords = h(
      "button",
      { class: "btn danger", type: "button", onClick: (event) => clearRecords_(event.currentTarget) },
      "清空全部记录",
    );
    const clearFiles = h(
      "button",
      { class: "btn danger", type: "button", onClick: (event) => clearFiles_(event.currentTarget) },
      "清空缓存文件",
    );

    return card("维护", "记录与文件是两回事：删记录不会删文件，删文件也不会删记录", [
      h("p", {
        class: "card-note",
        text: "「清理过期文件」按「维护」组里的缓存保留时长处理，只删没被访问过的旧文件，不影响记录。",
      }),
      h("div", { class: "toolbar" }, [cleanupButton, h("div", { class: "spacer" }), clearRecords, clearFiles]),
    ]);
  }

  async function reload() {
    try {
      await load(false);
      render();
    } catch (error) {
      toast(`加载失败：${error.message}`, "err");
    }
  }

  /* ---------------- 详情 ---------------- */

  function openDetail(record) {
    const frame = h("div", { class: "shot-frame" }, [
      record.card_exists
        ? h("div", { class: "empty" }, [spinner(), " 正在载入卡片预览…"])
        : placeholder("卡片文件已被缓存清理删除"),
    ]);

    const detail = record.detail || {};
    const rows = [
      ["平台", `${record.platform_display || record.platform} · ${record.content_type}`],
      ["标题", record.title || "—"],
      ["作者", record.author || "—"],
      ["来源", VIA_LABELS[record.via] || record.via],
      ["解析时间", fmtTime(record.created_at)],
      ["耗时", fmtMs(record.elapsed_ms)],
      ["链接", record.url || "—"],
      ["卡片文件", record.card_file || "—"],
      ["媒体文件", record.media_files && record.media_files.length ? record.media_files.join("、") : "—"],
    ];

    const actions = h("div", { class: "toolbar", style: { marginTop: "12px" } }, [
      h(
        "button",
        {
          class: "btn",
          type: "button",
          disabled: !record.card_exists,
          onClick: () => download(record),
        },
        "下载卡片原图",
      ),
      h(
        "button",
        {
          class: "btn danger",
          type: "button",
          onClick: async (event) => {
            const ok = await removeOne(record, event.currentTarget);
            if (ok) modal.close("deleted");
          },
        },
        "删除这条记录",
      ),
    ]);

    const modal = openModal({
      title: record.title || record.url || "解析记录",
      width: "720px",
      body: [
        h("div", { class: "two-col" }, [
          frame,
          h("dl", { class: "detail-grid" }, rows.flatMap(([key, value]) => [
            h("dt", { text: key }),
            h("dd", { text: String(value) }),
          ])),
        ]),
        detail.text
          ? h("div", { style: { marginTop: "12px" } }, [
              h("p", { class: "card-note", text: "正文节选" }),
              h("p", { style: { fontSize: "12px", margin: "0" }, text: detail.text }),
            ])
          : null,
        actions,
      ],
    });

    if (record.card_exists) {
      ctx.api
        .get("cache/thumbnail", { id: record.id })
        .then((result) => {
          clear(frame);
          frame.appendChild(h("img", { src: result.data_url, alt: "卡片预览" }));
        })
        .catch((error) => {
          clear(frame);
          frame.appendChild(placeholder(`预览加载失败：${error.message}`));
        });
    }
  }

  async function download(record) {
    if (!record.card_exists) {
      toast("卡片文件已被清理，无法下载", "err");
      return;
    }
    try {
      const name = (record.title || record.platform_display || "card").slice(0, 40);
      await ctx.api.download("cache/download", { id: record.id }, `${name}.png`);
      toast("已开始下载", "ok");
    } catch (error) {
      toast(`下载失败：${error.message}`, "err");
    }
  }

  async function removeOne(record, button) {
    const ok = await confirmDialog({
      title: "删除这条解析记录",
      body: `「${record.title || record.url || record.id}」将从记录列表移除。缓存文件会保留，之后仍可在文件层面清理。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return false;

    const restore = button ? withBusy(button, true, "删除中…") : () => {};
    try {
      const result = await ctx.api.post("cache/delete", { ids: [record.id] });
      toast(`已删除 ${result.removed} 条记录`, "ok");
      if (data.items.length === 1 && query.offset > 0) {
        query.offset = Math.max(0, query.offset - query.limit);
      }
      await reload();
      return true;
    } catch (error) {
      toast(`删除失败：${error.message}`, "err");
      return false;
    } finally {
      restore();
    }
  }

  /* ---------------- 维护操作 ---------------- */

  async function cleanup(button) {
    const restore = withBusy(button, true, "清理中…");
    try {
      const result = await ctx.api.post("cache/cleanup", {});
      toast(`已清理 ${result.removed_files} 个过期文件`, "ok");
      await reload();
    } catch (error) {
      toast(`清理失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  async function clearRecords_(button) {
    const ok = await confirmDialog({
      title: "清空全部解析记录",
      body: `将删除 ${data.total} 条记录（包括已筛选之外的全部记录）。缓存文件不会被删除。`,
      confirmText: "清空记录",
      danger: true,
    });
    if (!ok) return;
    const restore = withBusy(button, true, "清空中…");
    try {
      const result = await ctx.api.post("cache/clear", { scope: "records" });
      toast(`已删除 ${result.removed_records} 条记录`, "ok");
      query.offset = 0;
      await reload();
    } catch (error) {
      toast(`清空失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  async function clearFiles_(button) {
    const dir = (data.cache && data.cache.dir) || "缓存目录";
    const ok = await confirmDialog({
      title: "清空缓存文件",
      body: `将删除 ${dir} 下的全部缓存文件（卡片图片、下载的媒体、截图），并清空内存里的解析结果缓存。解析记录会保留，但记录里的卡片将无法再预览。`,
      confirmText: "清空文件",
      danger: true,
    });
    if (!ok) return;
    const restore = withBusy(button, true, "清空中…");
    try {
      const result = await ctx.api.post("cache/clear", { scope: "files" });
      toast(`已删除 ${result.removed_files} 个缓存文件`, "ok");
      await reload();
    } catch (error) {
      toast(`清空失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  return {
    async mount(node) {
      container = node;
      await load();
      render();
    },
    async refresh() {
      await reload();
    },
    unmount() {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = null;
    },
  };
}
