/* 页头彩蛋文案：「总览」标题与「刷新」按钮之间那行空白里的一句轮换文案。
 *
 * 节点在 index.html 的 .stage-head 静态区（不随 #stage-body 重绘），
 * 这里只负责「铺第一句 → 每 15s 淡出旧文案、淡入新文案」的循环。
 *
 * 三条硬约定：
 * - 关动效（html[data-motion="0"] 或系统 prefers-reduced-motion）时不轮换，
 *   静态停在当前文案；过渡动画由 CSS 侧停掉。
 * - 鼠标悬停时暂停轮换（所以 CSS 不给它设 pointer-events:none）。
 * - 文案写死在这里，不向用户开放配置：这是彩蛋位，不是设置项。
 */

const MESSAGES = [
  "小希今天也很棒~",
  "愿世间仍有希望……",
  "给娅娅喂个小蛋糕吧",
];

/** 轮换间隔（毫秒）；淡出过渡在 style.css 是 0.34s，换字延迟与此对齐。 */
const ROTATE_MS = 15000;
const SWAP_DELAY_MS = 360;

let timer = null;
let index = 0;

function motionOff() {
  if (document.documentElement.dataset.motion === "0") return true;
  return Boolean(
    window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
}

function apply(banner, line) {
  line.textContent = MESSAGES[index];
  banner.classList.remove("is-leaving");
}

function tick(banner, line) {
  // 关动效时保持当前文案不动；后台标签页同样不轮换（浏览器会节流定时器，
  // 攒下的 tick 在切回前台时连发，文案会连跳好几条）。
  if (motionOff() || document.hidden) return;
  if (banner.matches(":hover")) return;
  banner.classList.add("is-leaving");
  window.setTimeout(() => {
    index = (index + 1) % MESSAGES.length;
    apply(banner, line);
  }, SWAP_DELAY_MS);
}

/** 挂载横幅轮换。幂等：重复调用不会起第二个定时器。 */
export function initBanner() {
  const banner = document.getElementById("head-banner");
  const line = document.getElementById("banner-line");
  if (!banner || !line || timer) return;
  apply(banner, line);
  timer = window.setInterval(() => tick(banner, line), ROTATE_MS);
}
