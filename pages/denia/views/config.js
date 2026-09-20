/* 配置页：把 CONFIG_META 描述的全部配置项渲染成表单。
 *
 * 页面不再依赖 AstrBot 的原生插件配置面板（_conf_schema.json 里的条目都标了
 * invisible），所以这里要把 26 个配置项完整覆盖到，并做前端校验 + 脏值跟踪。
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
  let meta = { groups: [], items: [], platforms: [], problems: [] };
  let values = {};
  let draft = {};
  let filter = "";
  let collapsed = new Set();

  async function load() {
    ctx.setHead("加载中…");
    const payload = await ctx.api.get("config");
    meta = {
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

  function currentValue(key) {
    return Object.prototype.hasOwnProperty.call(draft, key) ? draft[key] : values[key];
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
    for (const group of meta.groups) {
      const items = group.keys
        .map(itemOf)
        .filter((item) => item && matchesFilter(item));
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

  function renderToolbar() {
    const search = h("input", {
      class: "input",
      type: "search",
      value: filter,
      placeholder: "搜索配置项（名称 / 键 / 说明）",
      onInput: (event) => {
        filter = event.target.value.trim();
        render();
      },
    });

    return card("配置项", `${meta.items.length} 项，保存后立即生效`, [
      h("div", { class: "toolbar" }, [
        h("div", { class: "grow" }, [search]),
        h(
          "button",
          {
            class: "btn",
            type: "button",
            onClick: () => {
              collapsed = collapsed.size ? new Set() : new Set(meta.groups.map((g) => g.name));
              render();
            },
          },
          collapsed.size ? "展开全部" : "收起全部",
        ),
      ]),
    ]);
  }

  function renderGroup(group, items) {
    const isCollapsed = collapsed.has(group.name);
    const head = h(
      "button",
      {
        class: "card-head",
        type: "button",
        style: { width: "100%", background: "transparent", border: "0", cursor: "pointer", textAlign: "left" },
        onClick: () => {
          if (isCollapsed) collapsed.delete(group.name);
          else collapsed.add(group.name);
          render();
        },
      },
      [
        h("h2", { text: `${isCollapsed ? "＋" : "－"} ${group.name}` }),
        h("span", { class: "sub", text: `${items.length} 项 · ${group.description || ""}` }),
      ],
    );

    const section = h("section", { class: "card" }, [head]);
    if (!isCollapsed) {
      section.appendChild(h("div", {}, items.map(renderField)));
    }
    return section;
  }

  function renderField(item) {
    const control = buildControl(item);
    const parts = [h("div", { class: "field-label" }, [
      item.label,
      h("code", { text: item.key }),
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

    if (item.type === "text") {
      return h("textarea", {
        class: "input",
        value: value === null || value === undefined ? "" : String(value),
        rows: 3,
        spellcheck: "false",
        onInput: (event) => setDraft(item.key, event.target.value),
      });
    }

    if (item.secret) {
      return secretInput(
        value === null || value === undefined ? "" : String(value),
        (next) => setDraft(item.key, next),
        item.placeholder || "",
      );
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
