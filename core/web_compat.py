"""AstrBot WebUI 请求/响应的版本兼容层。

背景：AstrBot 的插件 WebAPI 有两代形态，且**注册能力与响应工具的可用版本并不一致**：

- ``context.register_web_api`` —— 4.24.2 起可用
- ``astrbot.api.web``（``json_response`` / ``error_response`` / ``file_response``
  / ``request``）—— 4.27 起才提供

原先 :mod:`.webui` 在每个 handler 体内裸写 ``from astrbot.api.web import request``。
在 4.24.2 ~ 4.26.x 上，路由能注册成功、页面能打开，但**每一次接口调用都会在
handler 内部抛裸 ImportError** —— 对用户就是「页面全白，什么都点不动」。
之所以没在模块顶层导入，是因为顶层会直接让整个插件 import 失败。

本模块把差异收在一处：导入失败不抛异常，而是落到 quart 分支，两条路径对外
暴露**同一组函数、同一套行为**。

契约（两条路径必须完全一致，否则页面会一边好使一边白屏）：

- 成功响应体是**裸业务对象**，不套 ``{status, message, data}`` 信封。
  页面侧 ``pages/denia/app.js`` 直接读业务字段，bridge 也按「普通 JSON 原样
  交给页面」处理。
- 失败响应体是 ``{"status": "error", "message": ..., "data": {}}``，
  bridge 据此抛错，页面 ``catch`` 后拿 ``error.message`` 提示用户。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 依次尝试两种 HTTP 后端：优先新版 astrbot.api.web（4.27+），否则回退 quart。
# 两级 import 都要兜住 —— 连 quart 都没有时（例如插件被单独拿去做静态检查、
# 或宿主裁剪过依赖），本模块仍必须能 import 成功：它一旦抛 ImportError，
# main.py 里的 try/except 会把整个 WebUI 注册静默跳过，问题会被伪装成
# 「页面打不开」。真正的失败推迟到真正调用时，并以明确的 RuntimeError 呈现。
_BACKEND: str | None

try:  # AstrBot >= 4.27
    from astrbot.api.web import error_response as _api_error_response
    from astrbot.api.web import file_response as _api_file_response
    from astrbot.api.web import json_response as _api_json_response
    from astrbot.api.web import request as _api_request

    HAS_WEB_API = True
    _BACKEND = "astrbot.api.web"
except ImportError:  # AstrBot 4.24.2 ~ 4.26.x：只有 quart
    HAS_WEB_API = False
    try:
        import inspect

        from quart import jsonify as _quart_jsonify
        from quart import request as _api_request
        from quart import send_file as _quart_send_file

        # send_file 的下载文件名参数在 quart/werkzeug 版本间换过名
        # （attachment_filename → download_name），按签名挑一个能用的。
        _SEND_FILE_PARAMS = set(inspect.signature(_quart_send_file).parameters)
        _DOWNLOAD_NAME_PARAM = (
            "download_name"
            if "download_name" in _SEND_FILE_PARAMS
            else "attachment_filename"
        )
        _BACKEND = "quart"
    except ImportError:  # 连 quart 都没有：留空壳，调用时报错
        _BACKEND = None

_NO_BACKEND_HINT = (
    "既没有 astrbot.api.web（需 AstrBot 4.27+）也没有 quart，"
    "WebUI 无法处理 HTTP 请求；请确认 AstrBot 依赖完整"
)


def json_response(data: Any, status_code: int = 200) -> Any:
    """成功响应：裸业务对象（不套信封），与 :mod:`.webui` 的既有前端契约一致。"""
    if _BACKEND == "astrbot.api.web":
        return _api_json_response(data, status_code=status_code)
    if _BACKEND != "quart":
        raise RuntimeError(_NO_BACKEND_HINT)
    response = _quart_jsonify(data)
    response.status_code = status_code
    return response


def error_response(message: str, status_code: int = 400) -> Any:
    """失败响应：信封形状必须与新版一致，bridge 才能把它识别成错误并抛给页面。"""
    if _BACKEND == "astrbot.api.web":
        return _api_error_response(message, status_code=status_code)
    if _BACKEND != "quart":
        raise RuntimeError(_NO_BACKEND_HINT)
    response = _quart_jsonify({"status": "error", "message": message, "data": {}})
    response.status_code = status_code
    return response


def file_response(
    path: str | Path,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    as_attachment: bool = True,
) -> Any:
    """把本地文件作为下载返回。"""
    if _BACKEND == "astrbot.api.web":
        return _api_file_response(path, filename=filename, content_type=content_type)
    if _BACKEND != "quart":
        raise RuntimeError(_NO_BACKEND_HINT)

    kwargs: dict[str, Any] = {"as_attachment": as_attachment}
    if content_type:
        kwargs["mimetype"] = content_type
    if filename:
        kwargs[_DOWNLOAD_NAME_PARAM] = filename
    return _quart_send_file(str(path), **kwargs)


async def get_json_body() -> dict[str, Any]:
    """读取请求体 JSON。

    空体、非 JSON、以及顶层不是对象的请求体一律给 ``{}``：调用点普遍直接
    ``body.get(...)``，把 list/标量透出去会在 handler 里变成 AttributeError，
    500 的堆栈对排查毫无帮助。
    """
    if _BACKEND is None:
        raise RuntimeError(_NO_BACKEND_HINT)
    if _BACKEND == "astrbot.api.web":
        try:
            data = await _api_request.json(default=None)
        except TypeError:
            # 没有 default 形参的旧版。再分两种形态退：先当方法调，
            # 仍不成（说明它是个 awaitable 属性）就直接 await 属性本身。
            try:
                data = await _api_request.json()
            except TypeError:
                data = await _api_request.json
    else:
        try:
            # quart: request.json 是 async property，空体给 None、坏体会抛
            data = await _api_request.json
        except Exception:  # noqa: BLE001 —— 解析失败按「没传 body」处理
            data = None
    return data if isinstance(data, dict) else {}


def query_arg(name: str, default: str = "") -> str:
    """读 query 参数（新版叫 ``request.query``，quart 叫 ``request.args``）。"""
    if _BACKEND is None:
        raise RuntimeError(_NO_BACKEND_HINT)
    if _BACKEND == "astrbot.api.web":
        raw = _api_request.query.get(name)
    else:
        raw = _api_request.args.get(name)
    return default if raw is None else str(raw)
