import {h, clear, card, toast} from './ui.js';

const STATES = {running: '进行中', ok: '成功', skipped: '跳过', error: '失败', unknown: '待确认'};
export function createDiagnosticsPanel(ctx) {
  const content = h('div');
  const refresh = h('button', {class: 'btn tiny', type: 'button', text: '刷新', onClick: load});
  const node = card('解析诊断', '查看最近处理步骤；重启后清空，仅保留最近 100 条', [content], refresh);
  let busy = false;
  async function download(id) {
    try {
      const {report} = await ctx.api.post('diagnostics/export', {id});
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}));
      const link = h('a', {href: url, download: `denia-diagnostic-${id}.json`});
      document.body.appendChild(link);
      link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { toast(error.message || '导出失败', 'err'); }
  }
  async function load() {
    if (busy) return;
    busy = true; refresh.disabled = true;
    try {
      const data = await ctx.api.get('diagnostics');
      clear(content); node.hidden = false;
      if (!data.items?.length) content.appendChild(h('p', {class: 'card-note', text: '还没有诊断记录，发送分享链接或在上方解析一次即可。'}));
      for (const item of (data.items || []).slice(0, 100)) content.appendChild(h('details', {class: 'tool-drawer'}, [
        h('summary', {}, [h('strong', {text: `${item.platform_display || item.platform} · ${STATES[item.status] || '待确认'}`}),
          h('span', {text: `${item.via === 'chat' ? '聊天' : '网页'} · ${item.elapsed_ms} ms · ${new Date(item.started_at).toLocaleTimeString()}`})]),
        ...item.stages.map(stage => h('p', {class: 'card-note', text: `${stage.label}：${STATES[stage.state] || '待确认'}${stage.message ? ' · ' + stage.message : ''}`})),
        h('button', {class: 'btn tiny', type: 'button', text: '导出脱敏报告', onClick: () => download(item.id)}),
      ]));
    } catch { node.hidden = true; }
    finally { busy = false; refresh.disabled = false; }
  }
  return {node, load};
}
