"""B站扫码登录管理（组合服务）。

从 ``main.py`` 抽出的职责：

- 后台轮询任务表的登记与取消（``start`` / ``cancel``，幂等；取消只作用于
  未完成任务，已扫码成功并写入 Cookie 的登录不会被误删 ——「cancel 防迟到覆盖」）；
- 登录状态字典的读写与延迟回收（``state`` / ``_schedule_state_cleanup``，
  WebUI 是轮询读取的，任务刚结束就删会让最后一轮拿到「任务不存在」）；
- 二维码申请与渲染（``qr_create`` / ``render_qr_png``）与临时文件延迟清理
  （``_schedule_qr_cleanup``，避免与消息发送竞争）；
- Cookie 校验 / 提交 / 持久化 / 登出（``cookie_status`` / ``_commit_login_cookie`` /
  ``load_cookie`` / ``logout``）：提交前先校验候选值，失败绝不清掉旧登录态；
  登出要把内存、解析器持久化文件、``bili_cookie.json`` 与配置项四处一起清。

**状态属性仍留在插件实例上**（``_bili_login_tasks`` / ``_bili_login_states`` /
``_bili_cookie`` / ``_bili_cookie_file`` / ``_bili_ck_applied`` /
``_bili_http_session`` / ``_delayed_cleanup_tasks``）：``core/webui.py``、
``core/credentials/service.py``、生命周期（``initialize`` / ``terminate`` /
``apply_runtime_config``）与既有测试直接引用它们，改动它们等于强改几十处外部
私有属性。本服务组合插件对象、集中实现状态机与协议逻辑，插件侧保留同名薄委托。

不 import ``main``，避免循环依赖；稳定依赖 ``core/config`` / ``core/exception``。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import qrcode
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .config import get_config
from .exception import ParseException

# ========== B站扫码登录 API ==========
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_CODE_SCANNED = 86090
QR_CODE_EXPIRED = 86038
QR_CODE_SUCCESS = 0
QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5

# 登录状态在任务结束后还要留一会儿：WebUI 是**轮询**读取的，任务一结束就删，
# 最后一轮会拿到「登录任务不存在」而不是「登录成功」。
LOGIN_STATE_TTL = 300
# 状态字典的条数上限。WebUI 每点一次「获取二维码」都是一个随机 task_id，
# 原先只从 _bili_login_tasks 里 pop，状态永远留着 —— 点得越多字典越长。
LOGIN_STATE_MAX = 32


class BiliLoginManager:
    """B站扫码登录状态机 + 协议轮询 + Cookie 提交的组合服务。

    状态全部放在插件实例上（见模块 docstring），本服务只负责流程与状态转移；
    凡是可能被测试覆盖/替换的钩子（``_bili_check_cookie_valid`` /
    ``_bili_apply_cookie_to_parser`` / ``_schedule_login_state_cleanup`` /
    ``_schedule_qr_cleanup``），一律经 ``self._plugin.<名字>`` 在**调用时**读取。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    # ---------- 对外：webui 与聊天命令共用的实现 ----------

    async def qr_create(self) -> tuple[str, str]:
        """向 B站申请扫码登录二维码。

        Returns:
            (扫码 URL, qrcode_key)

        Raises:
            ParseException: 申请失败或返回内容不完整。
        """
        session = self._plugin._bili_http_session
        if not session or session.closed:
            session = aiohttp.ClientSession()
            self._plugin._bili_http_session = session

        async with session.get(
            BILI_QR_GENERATE_URL, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()

        if data.get("code") != 0:
            raise ParseException(f"获取二维码失败: {data.get('message', '未知错误')}")

        payload = data.get("data") or {}
        qrcode_url = payload.get("url")
        qrcode_key = payload.get("qrcode_key")
        if not qrcode_url or not qrcode_key:
            raise ParseException("B站未返回二维码地址或 qrcode_key")
        return str(qrcode_url), str(qrcode_key)

    @staticmethod
    def render_qr_png(data: str) -> bytes:
        """把文本渲染成 PNG 字节，供 WebUI 拼成 data URL 展示。"""
        buffer = io.BytesIO()
        qrcode.make(data).save(buffer, "PNG")
        return buffer.getvalue()

    def state(self, task_id: str) -> dict[str, Any]:
        """读取扫码登录任务的当前状态（WebUI 轮询用）。"""
        state = self._plugin._bili_login_states.get(task_id)
        if state is None:
            return {"state": "unknown", "message": "登录任务不存在或已结束"}
        return dict(state)

    async def cancel(self, task_id: str | None = None) -> bool:
        """停止后台扫码轮询，**不登出、不清除已保存的 Cookie**。

        Args:
            task_id: 指定任务；为空时取消所有仍在进行的任务（含聊天命令发起的）。

        幂等：任务不存在、已结束、重复取消都返回 True。只取消**未完成**的任务，
        因此已经扫码成功并写入 Cookie 的登录不会被误删。
        """
        tasks = getattr(self._plugin, "_bili_login_tasks", None) or {}
        if task_id:
            targets = [task_id]
        else:
            targets = [tid for tid, task in tasks.items() if not task.done()]

        pending: list[tuple[str, asyncio.Task]] = []
        for tid in targets:
            task = tasks.get(tid)
            state = self._plugin._bili_login_states.get(tid, {})
            if task is not None and not task.done() and state.get("state") not in {"success", "failed", "expired"}:
                task.cancel()
                pending.append((tid, task))
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        for tid, task in pending:
            # Cancelling before the coroutine starts never runs its finally block.
            if tasks.get(tid) is task:
                tasks.pop(tid, None)
                state = self._plugin._bili_login_states.get(tid)
                if state is not None and state.get("state") in {"waiting", "scanned"}:
                    state.update(state="cancelled", message="登录任务已取消")
                self._plugin._schedule_login_state_cleanup(tid)
        return True

    def start(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ) -> None:
        """启动后台轮询任务。

        Args:
            task_id: 任务标识，聊天命令用发送者 ID，WebUI 用随机串。
            qrcode_key: ``qr_create`` 返回的轮询凭据。
            notify_umo: 需要把登录结果发到聊天时填会话标识，WebUI 场景传 None。
            qr_path: 临时二维码文件，任务结束时删除（WebUI 不落盘，传 None）。
        """
        existing = self._plugin._bili_login_tasks.get(task_id)
        if existing is not None and not existing.done():
            return

        self._plugin._bili_login_states[task_id] = {
            "state": "waiting",
            "message": "等待扫码",
            "username": "",
            "uid": 0,
            "started_at": time.time(),
            "expires_in": QR_CODE_EXPIRE_TIME,
        }
        self._plugin._bili_login_tasks[task_id] = asyncio.create_task(
            self._poll_qr_login(
                task_id, qrcode_key, notify_umo=notify_umo, qr_path=qr_path
            )
        )

    async def cookie_status(self) -> dict[str, Any]:
        """检查当前 Cookie 是否仍有效。

        会真实请求 B站接口，仅在用户主动点「检测」时调用，不要放进总览的自动刷新里。
        """
        if not self._plugin._bili_cookie:
            return {"configured": False, "valid": False, "error": "尚未配置 Cookie"}
        result = await self._plugin._bili_check_cookie_valid()
        return {
            "configured": True,
            "valid": bool(result.get("valid")),
            "username": result.get("username", ""),
            "uid": result.get("uid", 0),
            "error": result.get("error", ""),
        }

    async def logout(self) -> bool:
        """清除本地保存的 B站 Cookie（不会撤销 B站侧的登录态）。

        要清三处，少一处都会「看起来清了、实际还在用」：
        主模块的 ``bili_cookie.json``、解析器自己持久化的 ``bilibili_cookies.json``、
        以及配置项 ``BILI_CK``（否则重建解析器时又被读回来）。

        清除前先停掉仍在轮询的扫码任务：否则它可能在清除之后才判定成功，
        把刚清掉的 Cookie 又写回内存与文件（「清了又自己回来」）。
        """
        await self.cancel()
        self._plugin._bili_cookie = ""
        # 同步「已生效的配置项值」：下面会把配置项也清空，不清这里的话
        # 下一次热更新会以为配置项「没变」而跳过（其实该走的分支已经走完了）
        self._plugin._bili_ck_applied = ""

        cleared = True
        parser = self._plugin.parsers.get("bilibili")
        if parser is not None and hasattr(parser, "clear_cookie"):
            try:
                if parser.clear_cookie() is False:
                    cleared = False
            except Exception:
                cleared = False
                logger.warning("[denia_share] 清除解析器 B站 Cookie 失败")

        pconfig = get_config()
        try:
            changed, errors = pconfig.apply_updates({"BILI_CK": ""})
            if errors or not changed and not pconfig.save():
                cleared = False
        except Exception:
            cleared = False
            logger.warning("[denia_share] 清除配置里的 B站 Cookie 失败", exc_info=True)

        try:
            self._plugin._bili_cookie_file.unlink(missing_ok=True)
            return cleared
        except OSError:
            logger.warning("[denia_share] 删除 bili_cookie.json 失败", exc_info=True)
            return False

    # ---------- 内部方法 ----------

    @staticmethod
    def _headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _poll_qr_login(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ):
        """轮询扫码结果，成功后写入并持久化 Cookie。

        状态写进 ``self._plugin._bili_login_states``，WebUI 通过 ``state`` 读取；
        聊天命令则靠 ``notify_umo`` 收结果。两条路径共用这一份实现。
        """
        state = self._plugin._bili_login_states.get(task_id)
        if state is None:
            state = {"state": "waiting", "message": "等待扫码", "username": "", "uid": 0}
            self._plugin._bili_login_states[task_id] = state

        start = time.time()
        try:
            while time.time() - start < QR_CODE_EXPIRE_TIME:
                session = self._plugin._bili_http_session
                if not session or session.closed:
                    # 这里 break 会跳过 while 的 else（只有「时间耗尽」才走 else），
                    # 不显式置状态的话 UI 会一直显示「等待扫码」直到 300 秒回收。
                    state["state"] = "failed"
                    state["message"] = "登录会话已关闭，请重新获取二维码"
                    break
                try:
                    async with session.get(
                        BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key},
                        headers=self._headers(), timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)
                if code == QR_CODE_EXPIRED:
                    state["state"] = "expired"
                    state["message"] = "二维码已过期，请重新获取"
                    break
                if code == QR_CODE_SCANNED:
                    state["state"] = "scanned"
                    state["message"] = "已扫码，请在手机上确认登录"
                elif code == QR_CODE_SUCCESS:
                    cookie_dict: dict[str, str] = {}
                    for header in set_cookie_headers:
                        part = header.split(";")[0].strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookie_dict[k.strip()] = v.strip()
                    if not cookie_dict:
                        state["state"] = "failed"
                        state["message"] = "B站未返回 Cookie，请重试"
                        break
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    # 先校验候选值再提交：旧 Cookie 可能还有效，没通过校验的候选值
                    # 绝不能顶掉它。校验只拿候选值发请求，不写 _bili_cookie。
                    result = await self._plugin._bili_check_cookie_valid(cookie_str)
                    if not result["valid"]:
                        state["state"] = "failed"
                        state["message"] = (
                            "Cookie 校验失败，已保留原登录态："
                            f"{result.get('error', '未知错误')}"
                        )
                        break
                    problem = await self._plugin._bili_commit_login_cookie(cookie_str)
                    if problem:
                        state["state"] = "failed"
                        state["message"] = problem
                        break
                    state["state"] = "success"
                    state["username"] = result.get("username", "")
                    state["uid"] = result.get("uid", 0)
                    state["message"] = "登录成功"
                    if notify_umo:
                        await self._plugin._bili_notify(
                            notify_umo,
                            f"🎉 登录成功！\n👤 用户: {result.get('username')}\n🆔 UID: {result.get('uid', 0)}",
                        )
                    break
                await asyncio.sleep(POLL_INTERVAL)
            else:
                state["state"] = "expired"
                state["message"] = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            state["state"] = "cancelled"
            state["message"] = "登录任务已取消"
            raise
        except Exception:
            logger.warning("[denia_share] B站扫码轮询未完成")
            state["state"] = "failed"
            state["message"] = "扫码登录未完成，请重新获取二维码"
        finally:
            self._plugin._bili_login_tasks.pop(task_id, None)
            # 状态不能跟着一起 pop：UI 还要读最终结果，见 _schedule_state_cleanup
            self._plugin._schedule_login_state_cleanup(task_id)
            if qr_path is not None:
                self._plugin._schedule_qr_cleanup(qr_path)

    def _schedule_state_cleanup(self, task_id: str) -> None:
        """登录任务结束后延迟回收状态，并把总量夹在上限内。

        两个理由：一是 WebUI 轮询读取，任务刚结束就删会让最后一轮拿到
        「登录任务不存在」而不是「登录成功」；二是 task_id 在 WebUI 侧是随机串，
        不清就会随点击次数无限增长（原先只 pop 了 ``_bili_login_tasks``，
        状态字典是只进不出的）。
        """
        states = self._plugin._bili_login_states
        tasks = self._plugin._bili_login_tasks
        delayed = self._plugin._delayed_cleanup_tasks
        if len(states) > LOGIN_STATE_MAX:
            ordered = sorted(
                states.items(),
                key=lambda kv: kv[1].get("started_at") or 0.0,
            )
            for old_id, _ in ordered[: len(ordered) - LOGIN_STATE_MAX]:
                # old_id == task_id 是「正在收尾的这条」：调用方在 finally 里已经先把
                # 本任务从 _bili_login_tasks 里 pop 掉了，所以下面那条豁免对它无效 ——
                # 一旦它恰好是最旧的一条，最终状态会被自己这次清理删掉，UI 再也读不到
                # 「登录成功」。所以本任务要单独豁免。
                if old_id == task_id or old_id in tasks:
                    continue  # 本任务与还在跑的任务都不能丢，UI 要读它的进度
                states.pop(old_id, None)

        async def _delayed_drop() -> None:
            try:
                await asyncio.sleep(LOGIN_STATE_TTL)
                states.pop(task_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("回收扫码登录状态失败", exc_info=True)

        delayed[:] = [task for task in delayed if not task.done()]
        delayed.append(asyncio.create_task(_delayed_drop()))

    def _schedule_qr_cleanup(self, qr_path: Path, delay: int = 120) -> None:
        """延迟删除二维码临时文件。

        二维码图片已经 yield 进消息管线，真正读取它的时刻在发送阶段，
        轮询一结束就删会与发送竞争、导致图片发不出去；所以推迟到
        ``delay`` 秒后再清理。顺带回收已完成的旧任务，避免列表无限增长。
        """
        delayed = self._plugin._delayed_cleanup_tasks

        async def _delayed_unlink() -> None:
            try:
                await asyncio.sleep(delay)
                qr_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("清理二维码临时文件失败", exc_info=True)

        delayed[:] = [task for task in delayed if not task.done()]
        delayed.append(asyncio.create_task(_delayed_unlink()))

    def _apply_cookie_to_parser(self, cookie_str: str) -> None:
        parser = self._plugin.parsers.get("bilibili")
        if parser and hasattr(parser, "update_cookie"):
            parser.update_cookie(cookie_str)
            logger.info("B站 Cookie 已应用到解析器")

    async def _check_cookie_valid(self, cookie_str: str | None = None) -> dict:
        cookie = self._plugin._bili_cookie if cookie_str is None else cookie_str
        session = self._plugin._bili_http_session
        if not cookie or not session:
            return {"valid": False, "error": "Cookie 为空或会话未初始化"}
        headers = {**self._headers(), "Cookie": cookie}
        try:
            async with session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                u = data["data"]
                return {"valid": True, "username": u.get("uname", ""), "uid": u.get("mid", 0)}
            return {"valid": False, "error": "登录态已失效或验证未通过"}
        except Exception:
            return {"valid": False, "error": "验证暂时失败，请稍后重试"}

    async def load_cookie(self) -> None:
        """启动时加载 B站 Cookie：**配置项优先**，持久化文件只是扫码登录的暂存。"""
        # 取值顺序要和 apply_runtime_config 与 parser._init_credential 保持一致，
        # 否则「网页上改了 BILI_CK」还是会被旧登录态压住。
        configured = str(get_config().BILI_CK or "")
        # 记下启动时生效的配置项值：apply_runtime_config 靠它判断「到底变没变」
        self._plugin._bili_ck_applied = configured
        if configured:
            self._plugin._bili_cookie = configured
            return
        try:
            cookie_file = self._plugin._bili_cookie_file
            if cookie_file.exists():
                saved = json.loads(cookie_file.read_text(encoding="utf-8")).get("cookie", "")
                if saved:
                    self._plugin._bili_cookie = saved
                    logger.info("已从持久化文件加载B站 Cookie")
        except Exception as e:
            logger.warning(f"加载B站 Cookie 失败: {e}")

    async def commit_login_cookie(self, cookie_str: str) -> str:
        """Commit a verified QR credential; return a public error on failure."""
        pconfig = get_config()
        old_cookie = str(pconfig.BILI_CK or "")
        cookie_file = self._plugin._bili_cookie_file
        staged = cookie_file.with_name(
            f".{cookie_file.name}.{time.time_ns()}.tmp"
        )
        config_attempted = False
        try:
            with staged.open("x", encoding="utf-8") as stream:
                json.dump({"cookie": cookie_str}, stream, ensure_ascii=False)
            config_attempted = True
            changed, errors = pconfig.apply_updates({"BILI_CK": cookie_str})
            if errors or not changed and not pconfig.save():
                raise OSError("configuration save failed")
            staged.replace(cookie_file)
        except Exception:
            if config_attempted:
                try:
                    changed, errors = pconfig.apply_updates({"BILI_CK": old_cookie})
                    if errors or not changed and not pconfig.save():
                        logger.warning("[denia_share] B站登录配置回滚未能持久保存")
                except Exception:
                    logger.warning("[denia_share] B站登录配置回滚未完成")
            return "登录凭据未能完整保存，请检查配置和存储后重试"
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                logger.warning("[denia_share] B站登录临时文件未能清理")
        self._plugin._bili_cookie = cookie_str
        self._plugin._bili_ck_applied = cookie_str
        self._plugin._bili_apply_cookie_to_parser(cookie_str)
        return ""

    async def notify(self, session: str, message: str) -> None:
        try:
            # session 期望是 unified_msg_origin（平台:消息类型:会话ID 三段式）；
            # 对历史调用方只传裸 QQ 号的情况做兼容兜底
            umo = session if ":" in session else f"default:FriendMessage:{session}"
            await self._plugin.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送消息到 {session} 失败: {e}")