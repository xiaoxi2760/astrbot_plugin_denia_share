/* 总览页：一眼看清插件当前状态，并承担「平台开关」和「B站扫码登录」两个最常用操作。 */

import {
  h,
  clear,
  card,
  statCard,
  pill,
  toast,
  confirmDialog,
  openModal,
  withBusy,
  fmtBytes,
  fmtTime,
  fmtMs,
  VIA_LABELS,
  emptyBox,
} from "../ui.js";

const BILI_STATE_TEXT = {
  waiting: "等待扫码",
  scanned: "已扫码，请在手机上确认",
  success: "登录成功",
  expired: "二维码已过期",
  failed: "登录失败",
  cancelled: "已取消",
  unknown: "任务已结束",
};

export function createOverviewView(ctx) {
  let data = null;
  let recent = [];
  let custom = null;
  let container = null;
  let loginPoll = null;

  function stopPoll() {
    if (loginPoll) {
      clearInterval(loginPoll);
      loginPoll = null;
    }
  }

  async function load(showBusy = true) {
    if (showBusy) ctx.setHead("加载中…");
    const [overview, cachePage, customParsers] = await Promise.all([
      ctx.api.get("overview"),
      ctx.api.get("cache", { limit: 3, offset: 0 }),
      // 只拿加载失败数做角标提示；列表拉不到（老版本后端）就静默省略
      ctx.api.get("custom_parsers").catch(() => null),
    ]);
    data = overview;
    recent = cachePage.items || [];
    custom = customParsers;
    ctx.setVersion(data.version ? `v${data.version}` : "");
    ctx.setBadge(cachePage.total || 0);
    ctx.setHead(data.send_error_messages ? "错误提示已开" : "", data.send_error_messages ? "warn" : "");
  }

  function render() {
    if (!container || !data) return;
    clear(container);

    const history = data.history || {};
    const cache = data.cache || {};

    container.appendChild(
      h("div", { class: "stat-grid" }, [
        statCard(
          "启用平台",
          `${data.enabled_count} / ${data.platforms.length}`,
          data.disabled_platforms.length
            ? `已禁用：${data.disabled_platforms.join("、")}`
            : "全部启用",
        ),
        statCard(
          "解析记录",
          `${history.count || 0}`,
          history.count
            ? `最近 ${fmtTime(history.newest_at)}`
            : "还没有记录",
        ),
        statCard(
          "缓存占用",
          cache.size_text || fmtBytes(cache.size_bytes || 0),
          `${cache.files || 0} 个文件 · 保留 ${cache.ttl_hours} 小时`,
        ),
        statCard(
          "卡片渲染",
          data.render.enabled ? "已开启" : "已关闭",
          data.render.enabled
            ? `${data.render.theme === "dark" ? "深色" : "浅色"} · ${data.render.layout} · ${data.render.width}px`
            : "当前回退为纯文本输出",
        ),
      ]),
    );

    const left = h("div", {}, [
      card("运行状态", null, [
        h("dl", { class: "detail-grid" }, [
          h("dt", { text: "插件版本" }),
          h("dd", { text: data.version ? `v${data.version}` : "未知" }),
          h("dt", { text: "B站清晰度" }),
          h("dd", { text: data.bili.quality }),
          h("dt", { text: "截图后端" }),
          h("dd", {
            text: `${data.screenshot.backend}（${data.screenshot.configured ? "已就绪" : "未配置"}）`,
          }),
          h("dt", { text: "链接兜底截图" }),
          h("dd", { text: data.screenshot.fallback ? "已开启" : "已关闭" }),
          h("dt", { text: "内存缓存" }),
          h("dd", {
            text: `解析结果 ${data.memory.result_cache} 条 · 卡片 ${data.memory.render_cache} 条`,
          }),
          // 「缓存目录」不在这里重复 —— 媒体发送卡里展示的是当前生效值（含回退后的）
          h("dt", { text: "数据目录" }),
          h("dd", { text: data.data_dir }),
        ]),
      ]),
    ]);

    left.appendChild(renderBiliCard());
    left.appendChild(renderMediaCard());

    // 自定义解析器的管理卡（目录 / 重载 / 报错详情）在「解析」页；
    // 这里只放一张紧凑开关卡，把右栏高度补齐到与左栏齐平
    const right = h("div", {}, [
      renderPlatformCard(),
      renderCustomParserMini(),
      renderRecentCard(),
    ]);

    container.appendChild(h("div", { class: "two-col" }, [left, right]));
  }

  /* ---------------- B站登录 ---------------- */

  function renderBiliCard() {
    const configured = data.bili.configured;
    const actions = h("div", { class: "toolbar" }, [
      h(
        "button",
        {
          class: "btn primary",
          type: "button",
          onClick: (event) => startLogin(event.currentTarget),
        },
        "扫码登录",
      ),
      h(
        "button",
        {
          class: "btn",
          type: "button",
          disabled: !configured,
          onClick: (event) => checkCookie(event.currentTarget),
        },
        "检测 Cookie",
      ),
      h(
        "button",
        {
          class: "btn danger",
          type: "button",
          disabled: !configured,
          onClick: (event) => logout(event.currentTarget),
        },
        "清除 Cookie",
      ),
    ]);

    const statusText = configured
      ? `已配置（${data.bili.cookie_length || 0} 字符）${data.bili.login_active ? " · 有登录任务进行中" : ""}`
      : "未配置，未登录时最高只能拿到 720P";

    return card(
      "B站登录",
      null,
      [
        h("p", { class: "card-note", text: statusText }),
        h("p", { class: "card-note", text: "扫码后用 B站App 确认，Cookie 会写入插件数据目录并持久化。" }),
        actions,
      ],
      configured ? pill("已配置", "ok") : pill("未配置", "warn"),
    );
  }

  /* ---------------- 媒体发送 ---------------- */

  function renderMediaCard() {
    const media = data.media || {};
    const mode = media.relay_enabled
      ? "中转链接"
      : media.shared_dir_configured
        ? "共享目录"
        : "插件数据目录";

    const rows = [
      ["发送方式", mode],
      ["缓存目录", media.cache_dir || data.cache.dir],
      ["中转链接", media.relay_enabled ? "已开启" : "已关闭"],
      ["回调地址", media.relay_callback || "未配置"],
      ["链接有效期", `${media.relay_ttl || 300} 秒`],
    ];

    // Docker 里两条路都没铺，视频就会因为协议端读不到本地路径而发不出去
    const risky = media.docker && !media.relay_enabled && !media.shared_dir_configured;

    const notes = [];
    // 回退是「静默降级」里最容易被漏掉的一种：功能看着还通，但分容器部署下
    // 协议端已经读不到视频了。所以既给整卡状态色，也把后果说清楚。
    const fellBack = media.cache_dir_source === "fallback";
    if (fellBack) {
      notes.push(
        `配置的共享缓存目录不可用，已回退到插件数据目录（${media.cache_dir || data.cache.dir}）。` +
          "分容器部署时协议端读不到这个路径，视频会发不出去。常见原因是目录不存在、" +
          "没挂载、或该目录已有内容却没有 .denia_share_cache 哨兵 —— " +
          "确认要把它当缓存目录，在该目录下建一个空的 .denia_share_cache 文件即可。",
      );
    }
    if (risky) {
      notes.push(
        "当前在容器里，但既没配「共享缓存目录」也没开「媒体中转」——" +
          "协议端读不到插件容器内的文件，视频会发不出去（图片和语音不受影响，它们会转成 base64）。" +
          "去「配置 → 媒体发送」二选一即可。",
      );
    }
    // 回退优先于 risky：它已经是「配了但没生效」的状态，比「没配」更需要处理
    const tone = fellBack ? "warn" : risky ? "warn" : "";

    return card(
      "媒体发送",
      media.docker ? "检测到容器环境" : "非容器环境",
      [
        h("dl", { class: "detail-grid" }, rows.flatMap(([key, value]) => [
          h("dt", { text: key }),
          h("dd", { text: String(value), style: { wordBreak: "break-all" } }),
        ])),
        ...notes.map((text) =>
          h("p", {
            class: "card-note",
            style: { marginTop: "8px", color: "var(--warn)" },
            text,
          }),
        ),
      ],
      media.relay_enabled
        ? pill("链接", "ok")
        : media.shared_dir_configured
          ? pill("共享目录", "ok")
          : pill(risky ? "可能有风险" : "默认", risky ? "warn" : ""),
      tone,
    );
  }

  async function checkCookie(button) {
    const restore = withBusy(button, true, "检测中…");
    try {
      const result = await ctx.api.get("bili/status", { check: 1 });
      if (result.valid) {
        toast(`Cookie 有效：${result.username || "未知用户"} (UID ${result.uid || 0})`, "ok");
      } else {
        toast(`Cookie 无效：${result.error || "未知原因"}`, "err");
      }
    } catch (error) {
      toast(`检测失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  async function logout(button) {
    const confirmed = await confirmDialog({
      title: "清除 B站 Cookie",
      body: "会删除插件数据目录里保存的 Cookie，并立即对解析器生效（B站侧的登录状态不受影响）。",
      confirmText: "清除",
      danger: true,
    });
    if (!confirmed) return;
    const restore = withBusy(button, true, "清除中…");
    try {
      await ctx.api.post("bili/logout", {});
      toast("已清除 B站 Cookie", "ok");
      await refresh();
    } catch (error) {
      toast(`清除失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  function startLogin(button) {
    const restore = withBusy(button, true, "获取中…");
    ctx.api
      .post("bili/qrcode", {})
      .then((result) => {
        restore();
        showQrModal(result);
      })
      .catch((error) => {
        restore();
        toast(`获取二维码失败：${error.message}`, "err");
      });
  }

  function showQrModal(payload) {
    stopPoll();
    const frame = h("div", { class: "shot-frame" }, [
      h("img", { src: payload.data_url, alt: "B站登录二维码" }),
    ]);
    const status = h("p", { class: "card-note", text: "等待扫码…（有效期约 3 分钟）" });
    const retryButton = h(
      "button",
      {
        class: "btn",
        type: "button",
        hidden: true,
        onClick: (event) => {
          modal.close("retry");
          startLogin(event.currentTarget);
        },
      },
      "重新获取二维码",
    );

    const modal = openModal({
      title: "B站扫码登录",
      width: "420px",
      body: [frame, status, h("div", { class: "toolbar" }, [retryButton])],
      onClose: stopPoll,
    });

    let taskId = payload.task_id;

    const tick = async () => {
      try {
        const state = await ctx.api.get("bili/qrcode/status", { task_id: taskId });
        status.textContent = BILI_STATE_TEXT[state.state] || state.message || state.state;
        if (state.state === "success") {
          stopPoll();
          status.textContent = `登录成功：${state.username || "未知用户"} （UID ${state.uid || 0}）`;
          toast("B站登录成功", "ok");
          setTimeout(() => {
            modal.close("success");
            refresh();
          }, 1200);
        } else if (["expired", "failed", "cancelled", "unknown"].includes(state.state)) {
          stopPoll();
          retryButton.hidden = false;
        }
      } catch (error) {
        status.textContent = `查询状态失败：${error.message}`;
      }
    };

    loginPoll = setInterval(tick, 2500);
    tick();
    void taskId;
  }

  /* ---------------- 平台开关 ---------------- */

  function platformChip(platform) {
    return h(
      "button",
      {
        class: `platform-chip ${platform.enabled ? "is-on" : ""}`.trim(),
        type: "button",
        title: platform.enabled ? "点击禁用" : "点击启用",
        onClick: (event) => togglePlatform(platform, event.currentTarget),
      },
      [h("span", { class: "chip-dot" }), platform.label],
    );
  }

  function renderPlatformCard() {
    const grid = h("div", { class: "platform-grid" });
    // 自定义平台不进这张卡：它们在下面有紧凑开关卡，管理（目录/重载/报错）在解析页
    for (const platform of data.platforms.filter((item) => !item.custom)) {
      grid.appendChild(platformChip(platform));
    }
    return card("平台开关", "点击切换，立即生效", [grid]);
  }

  /* ---------------- 自定义解析器（紧凑开关卡） ---------------- */

  /** 只显示「有没有 + 启停开关」，管理（目录 / 重载 / 报错详情）在「解析」页。 */
  function renderCustomParserMini() {
    const customs = (data.platforms || []).filter((item) => item.custom);
    const errors = (custom && custom.errors) || [];
    const body = [];

    if (customs.length) {
      // 与上面平台开关同一款 chip，复用 platformChip / togglePlatform
      body.push(h("div", { class: "platform-grid" }, customs.map(platformChip)));
    } else {
      body.push(
        h("p", {
          class: "card-note",
          text: "无 —— 在数据目录 custom_parsers/ 放 .py 文件即可扩展平台",
        }),
      );
    }
    if (errors.length) {
      body.push(
        h("p", {
          class: "parser-note warn",
          style: { cursor: "pointer" },
          title: "去「解析」页查看详情",
          text: `${errors.length} 个文件加载失败，点这里去「解析」页查看`,
          onClick: () => ctx.goTo("parse"),
        }),
      );
    }

    return card(
      "自定义解析器",
      customs.length ? `${customs.length} 个 · 点击切换启停` : null,
      body,
      h(
        "button",
        { class: "btn tiny", type: "button", onClick: () => ctx.goTo("parse") },
        "去管理",
      ),
      errors.length ? "err" : "",
    );
  }

  async function togglePlatform(platform, button) {
    button.disabled = true;
    try {
      const result = await ctx.api.post("platforms", {
        name: platform.name,
        enabled: !platform.enabled,
      });
      const stillEnabled = (result.runtime && result.runtime.platforms) || [];
      data.platforms = data.platforms.map((item) =>
        item.name === platform.name
          ? { ...item, enabled: stillEnabled.includes(item.name) }
          : item,
      );
      data.enabled_count = data.platforms.filter((item) => item.enabled).length;
      data.disabled_platforms = result.disabled_platforms || [];
      toast(`${platform.label} 已${platform.enabled ? "禁用" : "启用"}`, "ok");
      render();
    } catch (error) {
      toast(`切换失败：${error.message}`, "err");
      button.disabled = false;
    }
  }

  /* ---------------- 最近解析 ---------------- */

  function renderRecentCard() {
    const body =
      recent.length === 0
        ? emptyBox("还没有解析记录，去「解析」页手动试一条吧")
        : h(
            "div",
            { class: "list" },
            recent.map((record) =>
              // 整行可点跳缓存页：原先每行一个「去管理」按钮，五行五个同样的按钮，
              // 纯粹占高度 —— 卡头的「查看全部」保留，行内按钮删掉
              h("div", {
                class: "row",
                style: { cursor: "pointer" },
                title: "去缓存页管理",
                onClick: () => ctx.goTo("cache"),
              }, [
                h("div", { class: "row-main" }, [
                  h("div", {
                    class: "row-title",
                    text: record.title || record.url || "(无标题)",
                  }),
                  h("div", {
                    class: "row-sub",
                    text: `${record.platform_display} · ${record.content_type} · ${VIA_LABELS[record.via] || record.via} · ${fmtMs(record.elapsed_ms)}`,
                  }),
                ]),
                h("div", { class: "row-meta", text: fmtTime(record.created_at) }),
              ]),
            ),
          );

    return card("最近解析", "最近 3 条", [body], h(
      "button",
      { class: "btn tiny", type: "button", onClick: () => ctx.goTo("cache") },
      "查看全部",
    ));
  }

  return {
    async mount(node) {
      container = node;
      await load();
      render();
    },
    async refresh() {
      await load(false);
      render();
    },
    unmount() {
      stopPoll();
    },
  };
}
