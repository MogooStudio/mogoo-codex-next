"""One Responses HTTP request per relay attempt, with no CLI or retry fallback."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .adapter import AdapterError, POLICY, SCHEMA, _validate_result, worker_env
from .credentials import CredentialError, resolve_key
from .performance import clean_performance
from .wording import format_suggestions, wording_policy

HEADER_TIMEOUT = 45
READ_TIMEOUT = 45
TOTAL_TIMEOUT = 60
MAX_RESPONSE_BYTES = 2_000_000
MAX_EVENT_BYTES = 256_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(base_url: str) -> str:
    parts = urlsplit(base_url)
    # AI Code With documents /chatgpt/v1 for the Codex client and /v1 for
    # direct Responses clients. Preserve the selected origin and saved config.
    if (parts.scheme == "https" and parts.hostname in
            {"api.aicodewith.ai", "api.aicodewith.com", "api.with7.cn"}
            and parts.port in (None, 443) and parts.path.rstrip("/") == "/chatgpt/v1"):
        return urlunsplit((parts.scheme, parts.netloc, "/v1/responses", "", ""))
    return base_url.rstrip("/") + "/responses"


def _usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ("input_tokens", "output_tokens", "total_tokens")
              if type(value.get(key)) is int and value[key] >= 0}
    for source, name, target in (
        ("input_tokens_details", "cached_tokens", "cached_input_tokens"),
        ("input_tokens_details", "cache_write_tokens", "cache_write_input_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_output_tokens"),
    ):
        detail = value.get(source)
        if isinstance(detail, dict) and type(detail.get(name)) is int and detail[name] >= 0:
            result[target] = detail[name]
    return result


def _output_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            raise AdapterError("中转返回了无效结果。", code="output_schema")
        if item.get("type") not in ("message", "reasoning"):
            raise AdapterError("中转返回了未请求的工具调用。", code="tool_attempt")
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise AdapterError("建议模型未能提供建议，请手动重试。", code="relay_refusal")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


def _opener():
    # Match the effective desktop proxy configuration without changing os.environ.
    env = {key.lower(): value for key, value in worker_env().items()}
    proxies = {scheme: env.get(scheme + "_proxy") or env.get("all_proxy") for scheme in ("http", "https")}
    if any(value and urlsplit(value).scheme not in ("http", "https") for value in proxies.values()):
        raise AdapterError("HTTP 中转请使用 HTTP(S) 代理设置。", code="relay_proxy")
    return urllib.request.build_opener(urllib.request.ProxyHandler({k:v for k,v in proxies.items() if v}), _NoRedirect())


def _read_timeout(response, remaining):
    # CPython HTTPResponse wraps its socket in SocketIO. Bound each blocking read
    # by the remaining budget as well as idle time; fakes may expose only read().
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(max(.01, min(READ_TIMEOUT, remaining)))


@contextmanager
def _read_budget(response, deadline):
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    timer = None
    if sock is not None:
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # An idle timeout alone does not bound a peer that keeps sending tiny
        # fragments without completing a line. Interrupt only this owned socket.
        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()


def generate(config: dict, context: dict, *, on_first=None) -> dict:
    try:
        key = resolve_key(config)
    except CredentialError:
        raise AdapterError("中转 API Key 无法读取，请重新填写。", code="relay_key") from None
    if not key:
        raise AdapterError("请先填写中转 API Key。", code="relay_key")
    body = {"model": config["model"], "instructions": POLICY + wording_policy(config),
            "input": [{"role": "user", "content": [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False)}]}],
            "stream": True, "store": False, "tools": [],
            "reasoning": {"effort": config["reasoning_effort"]},
            "text": {"format": {"type": "json_schema", "name": "next_suggestions", "strict": True, "schema": SCHEMA}}}
    if config["service_tier"]:
        body["service_tier"] = config["service_tier"]
    request = urllib.request.Request(_endpoint(config["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "text/event-stream",
                 "User-Agent": "Mogoo-Codex-Next/" + __version__})
    usage = {}
    text = ""
    completed = False
    deadline = time.monotonic() + TOTAL_TIMEOUT
    started = time.perf_counter()
    performance = {}

    def mark(name):
        performance.setdefault(name, round((time.perf_counter() - started) * 1000, 3))

    def observe_suggestion():
        if "first_suggestion_ms" in performance:
            return
        # Strict output has one root field. raw_decode handles braces/escaped
        # quotes inside strings without requiring the unfinished array to close.
        prefix = re.match(r'\s*\{\s*"suggestions"\s*:\s*\[\s*', text)
        if prefix:
            try:
                first, _ = json.JSONDecoder().raw_decode(text, prefix.end())
                _validate_result({"suggestions": [first]})
            except (ValueError, TypeError, AdapterError):
                return
            mark("first_suggestion_ms")
            if on_first is not None and performance.get("response_mode") == "sse":
                # The consumer queues a read/verification; it must not wait for
                # that read on the HTTP stream's worker or start another request.
                preview = format_suggestions([first], config)[0]
                timing = clean_performance(performance)
                try:
                    on_first(preview, timing)
                except Exception:
                    # Preview delivery is best-effort; keep this stream open for
                    # final validation and usage without retrying the request.
                    pass

    def failure(message, code):
        error = AdapterError(message, usage=usage, code=code)
        error.performance = performance
        return error

    def consume(event):
        nonlocal text, usage, completed
        if not isinstance(event, dict):
            raise AdapterError("中转流格式无效。", code="relay_stream")
        kind = event.get("type")
        response = event.get("response")
        if isinstance(response, dict):
            usage = _usage(response.get("usage")) or usage
            if kind in ("response.completed", "response.failed", "response.incomplete"):
                performance.update(clean_performance({"service_tier": response.get("service_tier")}))
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") not in ("message", "reasoning"):
                raise AdapterError("中转返回了未请求的工具调用。", code="tool_attempt")
        if kind in ("response.refusal.delta", "response.refusal.done"):
            raise AdapterError("建议模型未能提供建议。", code="relay_refusal")
        if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            if event["delta"].strip():
                mark("first_text_ms")
            text += event["delta"]
            observe_suggestion()
        elif kind == "response.output_text.done" and isinstance(event.get("text"), str):
            text = event["text"]
            observe_suggestion()
        elif kind == "response.completed":
            if not isinstance(response, dict) or response.get("status", "completed") != "completed":
                raise AdapterError("中转请求未完成。", code="relay_failed")
            final_text = _output_text(response)
            text = final_text or text
            observe_suggestion()
            mark("completed_ms")
            completed = True
        elif kind in ("response.failed", "response.incomplete", "error"):
            raise AdapterError("中转请求未完成。", code="relay_failed")
        if len(text) > MAX_RESPONSE_BYTES:
            raise AdapterError("中转结果过大。", code="relay_size")

    try:
        # No redirect following, request retries, endpoint changes or CLI fallback.
        with _opener().open(request, timeout=min(HEADER_TIMEOUT, TOTAL_TIMEOUT)) as response, _read_budget(response, deadline):
            mark("headers_ms")
            performance["response_mode"] = "sse" if response.headers.get_content_type() == "text/event-stream" else "json"
            if response.headers.get_content_type() != "text/event-stream":
                _read_timeout(response, deadline - time.monotonic())
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AdapterError("中转结果过大。", code="relay_size")
                consume({"type": "response.completed", "response": json.loads(raw)})
            else:
                data = []
                event_bytes = total_bytes = 0
                while not completed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    _read_timeout(response, remaining)
                    raw = response.readline(MAX_EVENT_BYTES + 1)
                    if not raw:
                        break
                    total_bytes += len(raw)
                    event_bytes += len(raw)
                    if event_bytes > MAX_EVENT_BYTES or total_bytes > MAX_RESPONSE_BYTES:
                        raise AdapterError("中转结果过大。", code="relay_size")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line:
                        if data:
                            payload = "\n".join(data)
                            if payload != "[DONE]":
                                consume(json.loads(payload))
                        data.clear()
                        event_bytes = 0
            if time.monotonic() > deadline:
                raise TimeoutError()
            if not completed:
                raise AdapterError("中转流提前结束。", code="relay_stream")
            return {"suggestions": format_suggestions(_validate_result(json.loads(text)), config),
                    "usage": usage, "performance": performance}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        code = ("relay_auth" if status in (401, 403) else "relay_unsupported" if status in (404, 405)
                else "relay_rate_limit" if status == 429 else "relay_redirect" if 300 <= status < 400 else "relay_http")
        mark("headers_ms")
        raise failure("中转 HTTP 请求失败，请检查服务设置后重试。", code) from None
    except (TimeoutError, socket.timeout):
        raise failure("中转服务响应超时，请手动重试。", "relay_timeout") from None
    except urllib.error.URLError as exc:
        code = "relay_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "relay_network"
        raise failure("无法完成中转请求，请检查网络后重试。", code) from None
    except AdapterError as exc:
        exc.usage = usage or exc.usage
        exc.performance = performance
        raise
    except (UnicodeError, ValueError, TypeError, AttributeError):
        raise failure("中转返回了无效格式，请手动重试。", "output_json") from None
    except OSError:
        code = "relay_timeout" if time.monotonic() >= deadline else "relay_network"
        raise failure("无法完成中转请求，请检查网络后重试。", code) from None
    finally:
        mark("request_finished_ms")
