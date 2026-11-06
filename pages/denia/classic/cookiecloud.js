import {h, clear, card, toast} from './ui.js';

export function createCookieCloudPanel(ctx, onChange = async () => {}) {
  const detail = h('div');
  const replace = h('input', {type: 'checkbox'});
  const message = h('p', {class: 'card-note', text: '读取同步状态…'});
  const button = h('button', {class: 'btn', type: 'button', text: '立即同步', onClick: sync});
  const node = card('CookieCloud 登录态同步', '从你自建的服务导入选定平台', [
    h('p', {class: 'card-note', text: '先在「平台接入 → CookieCloud」填写连接信息、选择平台并保存。自动同步默认关闭，不会替你续期浏览器里的登录态。'}),
    h('label', {class: 'inline'}, [replace, '替换选定平台的现有登录态（本次）']),
    h('div', {class: 'toolbar'}, [button, message]), detail,
  ]);
  let busy = false;
  let supported = false;
  function show(data) {
    message.textContent = data.message || (data.configured ? '连接信息已配置，尚未同步' : '请先填写并保存连接信息');
    clear(detail);
    if (data.last_success) detail.appendChild(h('p', {class: 'card-note', text: `最近成功：${new Date(data.last_success).toLocaleString()}`}));
    for (const item of data.results || []) detail.appendChild(h('p', {class: 'card-note', text: `${item.label}：${item.message}`}));
    button.disabled = busy || !data.configured || !data.platforms?.length;
  }
  async function load() {
    try {
      const data = await ctx.api.get('cookiecloud');
      supported = typeof data?.configured === 'boolean';
      node.hidden = !supported;
      if (supported) show(data);
    } catch { supported = false; node.hidden = true; }
  }
  async function sync() {
    if (busy) return;
    busy = true; button.disabled = true; message.textContent = '正在同步…';
    try {
      const data = await ctx.api.post('cookiecloud/sync', {overwrite: replace.checked});
      replace.checked = false;
      show(data);
      await onChange();
      toast(data.message || '同步完成', data.results?.some(item => item.status === 'error') ? 'warn' : 'ok');
    } catch (error) { toast(error.message || '同步失败，请重试', 'err'); }
    finally { busy = false; await load(); }
  }
  return {node, load, get supported() { return supported; }};
}
