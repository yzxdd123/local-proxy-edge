"""把每条 HTTP 流立刻追加到 NDJSON。

输出路径只来自环境变量 PROXY_EDGE_NDJSON。这个插件不改系统代理、
证书库、网卡或路由；证书仍留在 mitmproxy 自己的配置目录。
"""

from __future__ import annotations

import base64
import json
import os
import time

from mitmproxy import http

OUT = os.environ.get("PROXY_EDGE_NDJSON", "").strip()
if not OUT:
    raise SystemExit("PROXY_EDGE_NDJSON is required")


def _body(data: bytes | None):
    if data is None:
        return None
    try:
        return {"enc": "utf8", "len": len(data), "v": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"enc": "base64", "len": len(data), "v": base64.b64encode(data).decode("ascii")}


def _hdrs(headers) -> list[list[str]]:
    return [[k, v] for k, v in headers.items(multi=True)]


class FlowSink:
    def _write(self, flow: http.HTTPFlow, error: str | None = None) -> None:
        req = flow.request
        resp = flow.response
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(flow.timestamp_start)),
            "method": req.method,
            "url": req.pretty_url,
            "http_version": req.http_version,
            "request_headers": _hdrs(req.headers),
            "request_body": _body(req.raw_content),
            "status": resp.status_code if resp else None,
            "response_headers": _hdrs(resp.headers) if resp else None,
            "response_body": _body(resp.raw_content) if resp else None,
            "error": error,
        }
        with open(OUT, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            handle.flush()

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.websocket is None:
            self._write(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        self._write(flow, error=str(flow.error))

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self._write(flow)


addons = [FlowSink()]
