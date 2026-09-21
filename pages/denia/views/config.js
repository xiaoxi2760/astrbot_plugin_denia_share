/* 配置页：把 CONFIG_META 描述的全部配置项按「大类 → 分组 → 子组」三级渲染成表单。
 *
 * 页面不再依赖 AstrBot 的原生插件配置面板（_conf_schema.json 里的条目都标了
 * invisible），所以这里要把全部配置项完整覆盖到，并做前端校验 + 脏值跟踪。
 *
 * 三级数据都来自 GET config：
 * - sections（大类）：顶部导航条，选中后只显示该大类下的分组；搜索时跨全部大类
 * - groups（分组）：可折叠卡片，卡头显示组内未保存数
 * - subgroup（子组）：组内第三级小标题（可选字段，没有就整组平铺）
 */

import {
  h,
  clear,
  card,
  toast,
  confirmDialog,
  withBusy,
  switchControl,
  secretInput,
} from "../ui.js";

export function createConfigView(ctx) {
  let container = null;
  let meta = { sections: [], groups: [], items: [], platforms: [], problems: [] };
  let values = {};
  let draft = {};
  let filter = "";
  let activeSection = "all";
  let collapsed = new Set();

  async function load() {
    ctx.setHead("加载中…");
    const payload = await ctx.api.get("config");
    meta = {
      sections: Array.isArray(payload.sections) ? payload.sections : [],
      groups: payload.groups || [],
      items: payload.items || [],
      platforms: payload.platforms || [],
      problems: payload.problems || [],
    };
    values = payload.values || {};
    draft = {};
    const overview = await ctx.api.get("overview").catch(() => null);
    if (overview && overview.version) ctx.setVersion(`v${overview.version}`);
  }

  function itemOf(key) {
    return meta.items.find((item) => item.key === key);
  }

  function groupOf(name) {
    return meta.groups.find((group) => group.name === name);
  }

  function hasDraft(key) {
    return Object.prototype.hasOwnProperty.call(draft, key);
  }

  function currentValue(key) {
    return hasDraft(key) ? draft[key] : values[key];
  }

  function setDraft(key, value) {
    if (value === values[key]) {
      delete draft[key];
    } else {
      draft[key] = value;
    }
  }

  function dirtyKeys() {
    return Object.keys(draft);
  }

  function matchesFilter(item) {
    if (!filter) return true;
    const haystack = [item.key, item.label, item.hint, item.group]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(filter);
  }

  /** 当前应该展示的分组：选中某个大类时收窄到它；搜索时始终跨全部大类。 */
  function visibleGroups() {
    if (!meta.sections.length || filter || activeSection === "all") {
      return meta.groups;
    }
    const section = meta.sections.find((s) => s.key === activeSection);
    if (!section) return meta.groups;
    const allowed = new Set(section.groups);
    return meta.groups.filter((group) => allowed.has(group.name));
  }

  function sectionDirtyCount(section) {
    const groups = new Set(section.groups);
    return dirtyKeys().filter((key) => {
      const item = itemOf(key);
      return item && groups.has(item.group);
    }).length;
  }

  function render() {
    if (!container) return;
    clear(container);

    if (meta.problems && meta.problems.length) {
      container.appendChild(
        card("配置契约有问题", "请先把下面这些改好，否则保存的值可能被丢弃", [
          ...meta.problems.map((text) =>
            h("p", { class: "card-note", style: { color: "var(--err)" }, text }),
          ),
        ]),
      );
    }

    container.appendChild(renderToolbar());

    let rendered = 0;
    for (const group of visibleGroups()) {
      const items = group.keys.map(itemOf).filter((item) => item && matchesFilter(item));
      if (!items.length) continue;
      rendered += items.length;
      container.appendChild(renderGroup(group, items));
    }

    if (rendered === 0) {
      container.appendChild(
        card("没有匹配的配置项", null, [
          h("p", { class: "card-note", text: `没有找到包含「${filter}」的配置项。` }),
        ]),
      );
    }

    container.appendChild(renderSaveBar());
  }

  /* ---------------- 工具条（搜索 + 折叠 + 大类导航） ---------------- */

  function collapseAllState() {
    const names = visibleGroups().map((group) => group.name);
    return names.length > 0 && names.every((name) => collapsed.has(name));
  }

  function renderToolbar() {
    const search = h("input", {
      class: "input",
      type: "search",
      value: filter,
      placeholder: "搜索配置项（名称 / 键 / 说明），跨全部大类",
      onInput: (event) => {
        // haystack 已转小写，filter 必须同步转，否则搜大写键名（如 RENDER）永远落空
        filter = event.target.value.trim().toLowerCase();
        render();
      },
    });

    const section = meta.sections.find((s) => s.key === activeSection);
    const subtitle = filter
      ? `搜索「${filter}」· 跨全部大类`
      : section
        ? `${section.label} · ${section.description}`
        : `${meta.items.length} 项，保存后立即生效`;

    const toolbar = card("配置项", subtitle, [
      h("div", { class: "toolbar" }, [
        h("div", { class: "grow" }, [search]),
        h(
          "button",
          {
            class: "btn",
            type: "button",
            onClick: () => {
              const names = visibleGroups().map((group) => group.name);
              if (collapseAllState()) {
                names.forEach((name) => collapsed.delete(name));
              } else {
                names.forEach((name) => collapsed.add(name));
              }
              render();
            },
          },
          collapseAllState() ? "展开全部" : "收起全部",
        ),
      ]),
      renderSectionTabs(),
    ]);
    // 吸顶：翻长列表时搜索与大类导航始终在手边（stage-body 是滚动容器）
    toolbar.classList.add("cfg-toolbar");
    return toolbar;
  }

  function renderSectionTabs() {
    if (!meta.sections.length) return null;

    const tab = (key, label, count, dirty) =>
      h(
        "button",
        {
          type: "button",
          class: activeSection === key ? "is-active" : "",
          onClick: () => {
            if (activeSection === key) return;
            activeSection = key;
            render();
          },
        },
        [
          `${label} ${count}`,
          dirty
            ? h("span", {
                class: "seg-badge",
                title: `${dirty} 项未保存`,
                text: String(dirty),
              })
            : null,
        ],
      );

    const tabs = [tab("all", "全部", meta.items.length, dirtyKeys().length)];
    for (const section of meta.sections) {
      const count = section.groups.reduce((sum, name) => {
        const group = groupOf(name);
        return sum + (group ? group.keys.length : 0);
      }, 0);
      tabs.push(tab(section.key, section.label, count, sectionDirtyCount(section)));
    }
    return h("div", { class: "cfg-tabs-row" }, [h("div", { class: "seg cfg-tabs" }, tabs)]);
  }

  /* ---------------- 分组卡片（第二级） ---------------- */

  function renderGroup(group, items) {
    const isCollapsed = collapsed.has(group.name);
    const dirtyCount = group.keys.filter(hasDraft).length;
    const section = meta.sections.find((s) => s.groups.includes(group.name));

    const subParts = [`${items.length} 项`];
    // 搜索时跨大类展示，卡头标出归属大类，免得找完不知道改的是哪一类
    if (filter && section) subParts.push(section.label);
    if (group.description) subParts.push(group.description);

    const head = h(
      "button",
      {
        class: "card-head cfg-group-head",
        type: "button",
        onClick: () => {
          if (isCollapsed) collapsed.delete(group.name);
          else collapsed.add(group.name);
          render();
        },
      },
      [
        h("h2", {}, [
          h("span", { class: `chev${isCollapsed ? " closed" : ""}` }),
          group.name,
        ]),
        h("span", { class: "sub", text: subParts.join(" · ") }),
        dirtyCount
          ? h("span", { class: "cfg-dirty", text: `${dirtyCount} 项未保存` })
          : null,
      ],
    );

    const sectionNode = h("section", { class: "card" }, [head]);
    if (!isCollapsed) {
      sectionNode.appendChild(renderGroupBody(items));
    }
    return sectionNode;
  }

  /** 组内渲染：带 subgroup 的按子组（第三级）分段，没有子组的项排在最前。 */
  function renderGroupBody(items) {
    const body = h("div");
    if (!items.some((item) => item.subgroup)) {
      for (const item of items) body.appendChild(renderField(item));
      return body;
    }
    const order = [];
    const buckets = new Map();
    for (const item of items) {
      const sub = item.subgroup || "";
      if (!buckets.has(sub)) {
        buckets.set(sub, []);
        order.push(sub);
      }
      buckets.get(sub).push(item);
    }
    for (const sub of order) {
      if (sub) body.appendChild(h("div", { class: "cfg-subhead", text: sub }));
      for (const item of buckets.get(sub)) body.appendChild(renderField(item));
    }
    return body;
  }

  /* ---------------- 配置项（字段行） ---------------- */

  function renderField(item) {
    const control = buildControl(item);
    const parts = [h("div", { class: "field-label" }, [
      item.label,
      h("code", { text: item.key }),
      hasDraft(item.key) ? h("span", { class: "cfg-dirty", text: "已改" }) : null,
    ])];

    const body = h("div", { class: "field-body" }, [control]);
    if (item.hint) body.appendChild(h("div", { class: "field-hint", text: item.hint }));
    parts.push(body);

    return h("div", { class: "field" }, parts);
  }

  function buildControl(item) {
    const value = currentValue(item.key);

    if (item.type === "bool") {
      return switchControl(Boolean(value), (next) => {
        setDraft(item.key, next);
        render();
      });
    }

    if (item.type === "int") {
      const input = h("input", {
        class: "input",
        type: "number",
        value: value === null || value === undefined ? "" : String(value),
        min: item.min !== undefined ? String(item.min) : null,
        max: item.max !== undefined ? String(item.max) : null,
        style: { maxWidth: "160px" },
        onInput: (event) => {
          const raw = event.target.value;
          setDraft(item.key, raw === "" ? "" : Number(raw));
        },
      });
      return h("div", { class: "inline" }, [
        input,
        item.unit ? h("span", { class: "unit", text: item.unit }) : null,
      ]);
    }

    if (item.type === "select") {
      const options = item.options || [];
      const labels = item.labels || [];
      return h(
        "select",
        {
          class: "select",
          style: { maxWidth: "320px" },
          onChange: (event) => setDraft(item.key, event.target.value),
        },
        options.map((option, index) =>
          h("option", {
            value: option,
            text: labels[index] || option,
            selected: option === value ? true : null,
          }),
        ),
      );
    }

    // secret 判断必须放在 text 之前：BILI_CK / XHS_CK / PIXIV_CK 都是
    // type="text" + secret=true（长 Cookie 想要多行输入框），原先会先命中下面的
    // textarea 分支 → 遮罩与「显示/隐藏」按钮对它们完全失效，Cookie 明文显示在页面上。
    if (item.secret) {
      return secretInput(
        value === null || value === undefined ? "" : String(value),
        (next) => setDraft(item.key, next),
        item.placeholder || "",
      );
    }

    if (item.type === "text") {
      return h("textarea", {
        class: "input",
        value: value === null || value === undefined ? "" : String(value),
        rows: 3,
        spellcheck: "false",
        onInput: (event) => setDraft(item.key, event.target.value),
      });
    }

    return h("input", {
      class: "input",
      type: "text",
      value: value === null || value === undefined ? "" : String(value),
      placeholder: item.placeholder || "",
      spellcheck: "false",
      onInput: (event) => setDraft(item.key, event.target.value),
    });
  }

  /* ---------------- 保存栏 ---------------- */

  function renderSaveBar() {
    const keys = dirtyKeys();
    const text = keys.length
      ? `已修改 ${keys.length} 项：${keys.slice(0, 3).join("、")}${keys.length > 3 ? " 等" : ""}`
      : "没有未保存的改动";

    return h("div", { class: "savebar" }, [
      h("span", { class: "grow", text }),
      h(
        "button",
        {
          class: "btn",
          type: "button",
          disabled: keys.length === 0,
          onClick: () => {
            draft = {};
            render();
          },
        },
        "放弃更改",
      ),
      h(
        "button",
        {
          class: "btn danger",
          type: "button",
          onClick: (event) => resetAll(event.currentTarget),
        },
        "恢复默认",
      ),
      h(
        "button",
        {
          class: "btn primary",
          type: "button",
          disabled: keys.length === 0,
          onClick: (event) => save(event.currentTarget),
        },
        `保存更改${keys.length ? `（${keys.length}）` : ""}`,
      ),
    ]);
  }

  async function save(button) {
    const keys = dirtyKeys();
    if (!keys.length) return;

    const valuesToSave = {};
    for (const key of keys) valuesToSave[key] = draft[key];

    const restore = withBusy(button, true, "保存中…");
    try {
      const result = await ctx.api.post("config", { values: valuesToSave });
      values = result.values || values;
      draft = {};

      if (result.errors && result.errors.length) {
        toast(`部分配置未保存：${result.errors.join("；")}`, "err");
      } else if (result.changed && result.changed.length) {
        toast(`已保存 ${result.changed.length} 项配置`, "ok");
      } else {
        toast("配置没有变化", "");
      }
      render();
    } catch (error) {
      toast(`保存失败：${error.message}`, "err");
    } finally {
      restore();
    }
  }

  async function resetAll(button) {
    const ok = await confirmDialog({
      title: "恢复默认配置",
      // 文案必须与后端一致：reset_config 除了重置配置项，还会调 bili_logout() 把
      // B 站扫码登录态一起清掉（它不在配置项里，单独存在插件数据目录）。
      // 两个提交曾各修了这句承诺的一侧 —— 代码清了、文案说没清，方向相反。
      body:
        "所有配置项都会回到默认值，包括 Cookie、Token 与代理设置；" +
        "B 站扫码登录保存的登录态（不在配置项里）也会一并清除。这一步不能撤销。",
      confirmText: "恢复默认",
      danger: true,
    });
    if (!ok) return;

    const restore = withBusy(button, true, "恢复中…");
    try {
      const result = await ctx.api.post("config/reset", {});
      values = result.values || values;
      draft = {};
      toast(`已恢复默认（改动 ${result.changed ? result.changed.length : 0} 项）`, "ok");
      render();
    } catch (error) {
      toast(`恢复失败：${error.message}`, "err");
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
      await load();
      render();
    },
  };
}
