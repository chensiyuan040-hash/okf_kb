"""webio.py — HTTP 小工具(访问层共用:重试/超时/Basic/PAT;stdlib,零第三方依赖)。

内网离线部署:只用 urllib;重试只针对网络错误与 5xx——4xx 是配置/凭据问题,
原样抛出(带响应体摘要),不做无谓重试。
"""
from __future__ import annotations

import base64
import time
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes = b""):
        self.status = status
        self.url = url
        self.body = body
        snippet = body[:200].decode("utf-8", "replace") if body else ""
        super().__init__(f"HTTP {status} {url} {snippet}")


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    basic: tuple[str, str] | None = None,
    bearer: str | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 1.5,
) -> tuple[int, dict[str, str], bytes]:
    """发一次请求。返回 (status, headers, body)。网络错误/5xx 重试 retries 次。"""
    hdrs = dict(headers or {})
    if basic:
        token = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        hdrs["Authorization"] = f"Basic {token}"
    if bearer:
        hdrs["Authorization"] = f"Bearer {bearer}"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp else b""
            if e.code >= 500 and attempt < retries:
                last_err = HttpError(e.code, url, body)
            else:
                raise HttpError(e.code, url, body) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                last_err = e
            else:
                raise
        time.sleep(backoff * (attempt + 1))
    raise last_err  # pragma: no cover —— 循环内必抛


def get_json(url: str, **kw) -> dict:
    import json

    status, _, body = request("GET", url, **kw)
    return json.loads(body.decode("utf-8"))
