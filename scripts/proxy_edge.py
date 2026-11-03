"""在 127.0.0.1 上复用或拉起 mitmdump，并打开只走该代理的独立 Edge。

不修改系统代理、Windows 证书库、网卡或路由。登录由用户自己在窗口里完成。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import winreg
from pathlib import Path

LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 8888
APP_NAME = "local-proxy-edge"

PASSWORD_KEY = re.compile(
    r"^(pwd|password|passwd|passwordenc|smscode|sms_code|verificacode|"
    r"verifycode|verify_code|captcha|imagecode|imgcode)$",
    re.IGNORECASE,
)
TOKEN_KEYS = {
    "accesstoken": "accessToken",
    "refreshtoken": "refreshToken",
    "access_token": "accessToken",
    "refresh_token": "refreshToken",
}
LOGIN_PATH = re.compile(r"login|signin|sign-in|sso|qrcode|pollqr|oauth|authoriz|/token", re.IGNORECASE)
OUT_ASSIGN = re.compile(r"^OUT\s*=\s*r?([\"'])(.+?)\1", re.MULTILINE)
EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
DETACHED = 0x00000008 | 0x00000200 | 0x08000000


class ToolError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def addon_path() -> Path:
    return Path(__file__).resolve().with_name("live_dump.py")


def state_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / APP_NAME / "state.json"


def read_state() -> dict:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_system_proxy() -> dict:
    """只读当前用户的 WinINET 代理。调用方不得写回。"""
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
    ) as key:
        enable = _reg_value(key, "ProxyEnable")
        server = _reg_value(key, "ProxyServer")
    return {"proxy_enable": enable, "proxy_server": server}


def _reg_value(key, name):
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return value


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


def powershell_exe() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def list_processes() -> list[dict]:
    script = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('mitmdump.exe','mitmproxy.exe','mitmweb.exe','python.exe','pythonw.exe','msedge.exe')
} | ForEach-Object {
    [pscustomobject]@{
        pid = [int]$_.ProcessId
        ppid = [int]$_.ParentProcessId
        name = [string]$_.Name
        cmd = [string]$_.CommandLine
    } | ConvertTo-Json -Compress
}
"""
    completed = subprocess.run(
        [powershell_exe(), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ToolError(f"无法列出进程：{completed.stderr.strip() or completed.returncode}")
    rows = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _flag_value(cmd: str, *flags: str) -> str | None:
    if not cmd:
        return None
    for flag in flags:
        match = re.search(
            rf"(?:^|\s){re.escape(flag)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
            cmd,
        )
        if match:
            return next(part for part in match.groups() if part)
    return None


def user_data_dir_from_cmd(cmd: str) -> str | None:
    return _flag_value(cmd, "--user-data-dir")


def script_from_cmd(cmd: str) -> str | None:
    return _flag_value(cmd, "--scripts", "-s")


def same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def is_edge_browser(proc: dict) -> bool:
    return (proc.get("name") or "").lower() == "msedge.exe"


def is_edge_main(proc: dict) -> bool:
    cmd = proc.get("cmd") or ""
    return is_edge_browser(proc) and "--type=" not in cmd.lower() and bool(user_data_dir_from_cmd(cmd))


def ndjson_from_script(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = OUT_ASSIGN.search(text)
    if match:
        return match.group(2)
    return None


def find_edge(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ToolError(f"找不到 Edge：{path}")
        return path
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise ToolError("找不到 msedge.exe，请用 --edge 指定")


def find_mitmdump(explicit: str | None, hint: Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    env = os.environ.get("PROXY_EDGE_MITMDUMP")
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("mitmdump")
    if found:
        return Path(found)
    roots = [Path.cwd()]
    if hint:
        roots.append(hint)
    seen = set()
    for root in roots:
        for parent in [root, *root.parents]:
            key = os.path.normcase(str(parent))
            if key in seen:
                continue
            seen.add(key)
            candidate = parent / "research" / "capture_env" / "Scripts" / "mitmdump.exe"
            if candidate.is_file():
                return candidate
    return None


def edge_arguments(profile: Path, port: int, url: str) -> list[str]:
    return [
        f"--proxy-server=http://{LISTEN_HOST}:{port}",
        f"--user-data-dir={profile}",
        "--ignore-certificate-errors",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--window-size=1280,900",
        url,
    ]


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ToolError("目标网址必须是 http:// 或 https://")
    return url.strip()


def is_mitmdump_invocation(proc: dict, port: int) -> bool:
    name = (proc.get("name") or "").lower()
    cmd = proc.get("cmd") or ""
    if name not in {"mitmdump.exe", "mitmproxy.exe", "mitmweb.exe", "python.exe", "pythonw.exe"}:
        return False
    if name.startswith("python") and not re.search(r"mitmdump\.exe[\"\s]", cmd, re.IGNORECASE):
        return False
    listens = (
        f"--listen-port {port}" in cmd
        or f"--listen-port={port}" in cmd
        or re.search(rf"(?:^|\s)-p\s+{port}(?:\s|$)", cmd) is not None
    )
    if not listens:
        return False
    if "--listen-host" in cmd and "--listen-host 127.0.0.1" not in cmd and "--listen-host=127.0.0.1" not in cmd:
        return False
    return True


def describe_proxy(proc: dict) -> dict:
    script = script_from_cmd(proc.get("cmd") or "")
    ndjson = ndjson_from_script(Path(script)) if script else None
    session = str(Path(ndjson).parent) if ndjson else None
    return {
        "pid": proc.get("pid"),
        "name": proc.get("name"),
        "script": script,
        "ndjson": ndjson,
        "session_dir": session,
    }


def running_proxy(procs: list[dict], port: int) -> dict | None:
    matches = [proc for proc in procs if is_mitmdump_invocation(proc, port)]
    if not matches:
        return None

    def rank(proc: dict) -> tuple[int, int]:
        described = describe_proxy(proc)
        parsed = 0 if described.get("ndjson") else 1
        named = 0 if (proc.get("name") or "").lower() == "mitmdump.exe" else 1
        return (parsed, named)

    matches.sort(key=rank)
    return describe_proxy(matches[0])


def edge_for_profile(procs: list[dict], profile: Path) -> dict | None:
    for proc in procs:
        if not is_edge_main(proc):
            continue
        found = user_data_dir_from_cmd(proc.get("cmd") or "")
        if same_path(found, str(profile)):
            return {"running": True, "pid": proc.get("pid"), "profile": str(profile)}
    return None


def child_env(ndjson: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PROXY_EDGE_NDJSON"] = str(ndjson)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = env.get(key) or ""
        if f":{DEFAULT_PORT}" in value and ("127.0.0.1" in value or "localhost" in value):
            env.pop(key, None)
    return env


def resolve_session(requested: str | None, proxy: dict | None, create: bool = True) -> Path:
    if requested:
        session = Path(requested)
        if proxy and proxy.get("session_dir") and not same_path(str(session), proxy["session_dir"]):
            raise ToolError(
                "127.0.0.1 上的 mitmdump 正在把流量写入另一个会话目录。"
                "不要另开一套目录假装正在抓包；继续用当前会话，或先让用户确认可以停止现有代理。",
                code=3,
            )
        if create:
            session.mkdir(parents=True, exist_ok=True)
        return session
    if proxy and proxy.get("session_dir"):
        return Path(proxy["session_dir"])
    state = read_state()
    if state.get("session_dir"):
        return Path(state["session_dir"])
    stamp = time.strftime("%Y%m%d_%H%M")
    session = Path.cwd() / f"capture_live_{stamp}"
    if create:
        session.mkdir(parents=True, exist_ok=True)
    return session


def start_proxy(session: Path, port: int, mitmdump: Path) -> dict:
    if port_open(LISTEN_HOST, port):
        raise ToolError(f"{LISTEN_HOST}:{port} 已被占用，但没有识别到可复用的 mitmdump", code=3)
    ndjson = session / "flows.ndjson"
    flow_file = session / "flows.flow"
    log_path = session / "mitmdump.out.log"
    wfile = f"+{flow_file}" if flow_file.exists() else str(flow_file)
    cmd = [
        str(mitmdump),
        "--listen-host",
        LISTEN_HOST,
        "--listen-port",
        str(port),
        "--set",
        "block_global=true",
        "-s",
        str(addon_path()),
        "-w",
        wfile,
    ]
    log_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        cwd=str(session),
        env=child_env(ndjson),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        creationflags=DETACHED,
        close_fds=True,
    )
    log_handle.close()
    for _ in range(40):
        if port_open(LISTEN_HOST, port):
            break
        if process.poll() is not None:
            raise ToolError(f"mitmdump 已退出，日志：{log_path}", code=4)
        time.sleep(0.25)
    else:
        raise ToolError(f"mitmdump 没有在 {LISTEN_HOST}:{port} 监听，日志：{log_path}", code=4)
    return {
        "pid": process.pid,
        "ndjson": str(ndjson),
        "session_dir": str(session),
        "script": str(addon_path()),
        "log": str(log_path),
        "started": True,
    }


def ensure_proxy(session: Path, port: int, mitmdump: str | None, dry_run: bool) -> dict:
    procs = list_processes()
    current = running_proxy(procs, port)
    if current:
        current["started"] = False
        current["reused"] = True
        return current
    if port_open(LISTEN_HOST, port):
        raise ToolError(f"{LISTEN_HOST}:{port} 已有连接，但进程不是 mitmdump。不会结束它。", code=3)
    binary = find_mitmdump(mitmdump, session)
    if binary is None:
        raise ToolError("找不到 mitmdump。安装 mitmproxy，或用 --mitmdump 指定 mitmdump.exe")
    plan = {
        "started": False,
        "reused": False,
        "dry_run": dry_run,
        "mitmdump": str(binary),
        "listen": f"{LISTEN_HOST}:{port}",
        "session_dir": str(session),
        "ndjson": str(session / "flows.ndjson"),
        "script": str(addon_path()),
    }
    if dry_run:
        return plan
    started = start_proxy(session, port, binary)
    started["reused"] = False
    return started


def powershell_single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def start_edge(edge: Path, profile: Path, port: int, url: str) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    args = edge_arguments(profile, port, url)
    quoted = ",".join(powershell_single(item) for item in args)
    command = "Start-Process -FilePath {edge} -ArgumentList @({args})".format(
        edge=powershell_single(str(edge)),
        args=quoted,
    )
    completed = subprocess.run(
        [powershell_exe(), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ToolError(f"打开 Edge 失败：{completed.stderr.strip() or completed.stdout.strip()}", code=4)


def stop_edge_profile(procs: list[dict], profile: Path) -> list[int]:
    pids = []
    for proc in procs:
        if not is_edge_browser(proc):
            continue
        found = user_data_dir_from_cmd(proc.get("cmd") or "")
        if same_path(found, str(profile)):
            pid = int(proc["pid"])
            if pid not in pids:
                pids.append(pid)
    main_pids = [
        int(proc["pid"])
        for proc in procs
        if is_edge_main(proc) and same_path(user_data_dir_from_cmd(proc.get("cmd") or ""), str(profile))
    ]
    targets = main_pids or pids
    for pid in targets:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    return targets


def hostname_of(value: str) -> str:
    text = value.strip()
    if "://" in text:
        host = urllib.parse.urlsplit(text).hostname or ""
    else:
        host = text.split("/")[0]
    return host.lower().rstrip(".")


def header_map(items) -> list[tuple[str, str]]:
    rows = []
    if not isinstance(items, list):
        return rows
    for item in items:
        if isinstance(item, list) and len(item) >= 2:
            rows.append((str(item[0]), str(item[1])))
    return rows


def header_values(items, name: str) -> list[str]:
    return [value for key, value in header_map(items) if key.lower() == name.lower() and value]


def decode_body(body) -> str | None:
    if not isinstance(body, dict) or body.get("enc") != "utf8":
        return None
    value = body.get("v")
    return value if isinstance(value, str) else None


def request_fields(body) -> list[str]:
    text = decode_body(body)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return [str(key) for key in data.keys()]
    if "=" in text and not text.lstrip().startswith("<"):
        return list(urllib.parse.parse_qs(text, keep_blank_values=True).keys())
    return []


def collect_tokens(value, found: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            canon = TOKEN_KEYS.get(str(key).lower())
            if canon and isinstance(item, str) and item.strip():
                found.setdefault(canon, item.strip())
            else:
                collect_tokens(item, found)
    elif isinstance(value, list):
        for item in value:
            collect_tokens(item, found)


def cookie_names(cookie: str) -> list[str]:
    names = []
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def redact(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if PASSWORD_KEY.match(str(key)):
                cleaned[str(key)] = "<redacted>"
            else:
                cleaned[str(key)] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def extract_flows(ndjson: Path, host: str) -> dict:
    wanted = hostname_of(host)
    if not wanted:
        raise ToolError("主机名不能为空")
    logins = []
    cookie = ""
    cookie_line = None
    matched = 0
    with ndjson.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if wanted not in line.lower():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = str(rec.get("url") or "")
            if hostname_of(url) != wanted:
                continue
            matched += 1
            cookies = header_values(rec.get("request_headers"), "cookie")
            if cookies:
                cookie = cookies[-1]
                cookie_line = line_no
            response_text = decode_body(rec.get("response_body"))
            response_json = None
            if response_text:
                try:
                    response_json = json.loads(response_text)
                except json.JSONDecodeError:
                    response_json = None
            tokens: dict[str, str] = {}
            if response_json is not None:
                collect_tokens(response_json, tokens)
            path = urllib.parse.urlsplit(url).path
            is_login = bool(tokens.get("accessToken")) or bool(LOGIN_PATH.search(path))
            if is_login and (tokens or response_json is not None):
                logins.append(
                    {
                        "source_line": line_no,
                        "time": rec.get("ts"),
                        "method": rec.get("method"),
                        "url": url,
                        "path": path,
                        "status": rec.get("status"),
                        "request_fields": request_fields(rec.get("request_body")),
                        "accessToken": tokens.get("accessToken"),
                        "refreshToken": tokens.get("refreshToken"),
                        "response": redact(response_json) if isinstance(response_json, (dict, list)) else None,
                    }
                )
    issued = [item for item in logins if item.get("accessToken")]
    latest = issued[-1] if issued else None
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_file": str(ndjson),
        "host": wanted,
        "matched_flows": matched,
        "accessToken": None if latest is None else latest.get("accessToken"),
        "refreshToken": None if latest is None else latest.get("refreshToken"),
        "cookie": cookie or None,
        "cookie_source_line": cookie_line,
        "cookie_names": cookie_names(cookie),
        "cookies": {wanted: cookie} if cookie else {},
        "how_to_use": {
            "header": "accessToken",
            "note": "把 accessToken 原样放入同名请求头，不要自行添加 Bearer；Cookie 只带给同一主机。以该站点真实请求为准。",
        },
        "logins": logins,
    }


def public_summary(result: dict, out: Path) -> dict:
    issued = [item for item in result["logins"] if item.get("accessToken")]
    latest = issued[-1] if issued else None
    summary = {
        "ok": True,
        "host": result["host"],
        "source_file": result["source_file"],
        "out": str(out),
        "matched_flows": result["matched_flows"],
        "login_count": len(issued),
        "latest_endpoint": None if latest is None else f"{latest['method']} {latest['path']}",
        "latest_status": None if latest is None else latest["status"],
        "latest_source_line": None if latest is None else latest["source_line"],
        "access_token_len": len(result.get("accessToken") or ""),
        "refresh_token_len": len(result.get("refreshToken") or ""),
        "cookie_len": len(result.get("cookie") or ""),
        "cookie_names": result.get("cookie_names") or [],
    }
    if not result.get("accessToken"):
        summary["warning"] = "没有筛到带 accessToken 的登录响应。窗口里完成登录后再提取。"
    return summary


def write_auth(result: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(out)


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def cmd_status(args) -> dict:
    procs = list_processes()
    proxy = running_proxy(procs, args.port)
    state = read_state()
    session = None
    if args.session:
        session = str(Path(args.session))
    elif proxy and proxy.get("session_dir"):
        session = proxy["session_dir"]
    elif state.get("session_dir"):
        session = state["session_dir"]
    profile = str(Path(session) / "edge_profile") if session else state.get("edge_profile")
    edge = edge_for_profile(procs, Path(profile)) if profile else None
    return {
        "ok": True,
        "listen": {
            "host": LISTEN_HOST,
            "port": args.port,
            "open": port_open(LISTEN_HOST, args.port),
            "proxy": proxy,
        },
        "session_dir": session,
        "edge": edge or {"running": False, "profile": profile},
        "system_proxy": read_system_proxy(),
        "system_proxy_policy": "read-only; this command does not modify it",
        "state_file": str(state_path()),
    }


def remember(session: Path, proxy: dict, port: int) -> None:
    write_state(
        {
            "session_dir": str(session),
            "ndjson": proxy.get("ndjson") or str(session / "flows.ndjson"),
            "edge_profile": str(session / "edge_profile"),
            "listen_host": LISTEN_HOST,
            "listen_port": port,
        }
    )


def cmd_open(args, relaunch: bool) -> dict:
    url = check_url(args.url)
    procs = list_processes()
    current = running_proxy(procs, args.port)
    session = resolve_session(args.session, current, create=not args.dry_run)
    profile = session / "edge_profile"
    if args.dry_run:
        proxy_plan = {
            "reused": bool(current),
            "listen": f"{LISTEN_HOST}:{args.port}",
            "session_dir": str(session),
            "ndjson": (current or {}).get("ndjson") or str(session / "flows.ndjson"),
        }
    else:
        proxy_plan = ensure_proxy(session, args.port, args.mitmdump, dry_run=False)
        if not proxy_plan.get("ndjson"):
            proxy_plan["ndjson"] = str(session / "flows.ndjson")
            proxy_plan["session_dir"] = str(session)
        remember(session, proxy_plan, args.port)
        procs = list_processes()
    existing = edge_for_profile(procs, profile)
    if existing and not args.force:
        return {
            "ok": True,
            "action": "edge-already-running",
            "edge_pid": existing["pid"],
            "profile": str(profile),
            "url_not_opened": url,
            "proxy": {key: proxy_plan.get(key) for key in ("pid", "reused", "started", "ndjson", "session_dir")},
            "note": "这个独立窗口还在。不要再开一个同配置窗口。用户说窗口已关或卡住时才使用 relaunch --force。",
        }
    plan = {
        "ok": True,
        "action": "relaunch" if relaunch else "open",
        "edge": str(find_edge(args.edge)),
        "arguments": edge_arguments(profile, args.port, url),
        "profile": str(profile),
        "proxy": proxy_plan,
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        return plan
    stopped = []
    if existing and args.force:
        stopped = stop_edge_profile(procs, profile)
        time.sleep(0.8)
    start_edge(find_edge(args.edge), profile, args.port, url)
    plan["stopped_pids"] = stopped
    plan["opened"] = True
    return plan


def cmd_extract(args) -> dict:
    state = read_state()
    procs = list_processes()
    proxy = running_proxy(procs, args.port)
    if args.ndjson:
        ndjson = Path(args.ndjson)
    elif proxy and proxy.get("ndjson"):
        ndjson = Path(proxy["ndjson"])
    elif state.get("ndjson"):
        ndjson = Path(state["ndjson"])
    else:
        raise ToolError("不知道 flows.ndjson 路径。请用 --ndjson 指定，或先打开代理会话。")
    if not ndjson.is_file():
        raise ToolError(f"找不到流量文件：{ndjson}")
    result = extract_flows(ndjson, args.host)
    if args.out:
        out = Path(args.out)
    else:
        session = ndjson.parent
        out = session / "auth" / result["host"] / "auth.json"
    write_auth(result, out)
    return public_summary(result, out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proxy_edge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--session")

    for name in ("open", "relaunch"):
        command = sub.add_parser(name)
        command.add_argument("--url", required=True)
        command.add_argument("--session")
        command.add_argument("--edge")
        command.add_argument("--mitmdump")
        command.add_argument("--force", action="store_true")
        command.add_argument("--dry-run", action="store_true")

    extract = sub.add_parser("extract")
    extract.add_argument("--host", required=True)
    extract.add_argument("--ndjson")
    extract.add_argument("--out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "status":
            payload = cmd_status(args)
        elif args.cmd == "open":
            payload = cmd_open(args, relaunch=False)
        elif args.cmd == "relaunch":
            payload = cmd_open(args, relaunch=True)
        elif args.cmd == "extract":
            payload = cmd_extract(args)
        else:
            raise ToolError(f"未知命令：{args.cmd}")
    except ToolError as exc:
        emit({"ok": False, "error": str(exc)})
        return exc.code
    emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
