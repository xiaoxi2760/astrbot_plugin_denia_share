/* Fixed local assets: no account information or third-party requests. */
import { sharedResourceUrl } from "../shared-resources.js";

const FILES = Object.freeze({
  bilibili: "bilibili.svg", douyin: "douyin.svg", kuaishou: "kuaishou.svg",
  weibo: "weibo.svg", xiaohongshu: "xiaohongshu.svg", twitter: "twitter.svg",
  github: "github.svg", pixiv: "pixiv.svg", steam: "steam.svg",
  acfun: "acfun.png", nga: "nga.png", xiaoheihe: "xiaoheihe.png",
  netease: "netease.png", qqmusic: "qqmusic.png",
});

export function platformIconUrl(id) {
  const file = Object.hasOwn(FILES, id) ? FILES[id] : "";
  return file ? sharedResourceUrl(`platforms/${file}`) : "";
}

export function createPlatformIcon(id) {
  const src = platformIconUrl(id);
  if (!src) return null;
  const img = document.createElement("img");
  img.src = src;
  img.alt = "";
  img.className = "platform-icon";
  img.width = img.height = 24;
  return img;
}
