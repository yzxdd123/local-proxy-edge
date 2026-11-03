"""不连接真实站点、不读取现有抓包文件。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import proxy_edge


def flow(method, url, request_body=None, response_body=None, request_headers=None, status=200):
    return {
        "ts": "2026-11-03T12:00:00+0800",
        "method": method,
        "url": url,
        "request_headers": request_headers or [],
        "request_body": None
        if request_body is None
        else {"enc": "utf8", "len": len(request_body), "v": request_body},
        "status": status,
        "response_headers": [],
        "response_body": None
        if response_body is None
        else {"enc": "utf8", "len": len(response_body), "v": response_body},
        "error": None,
    }


class ExtractTests(unittest.TestCase):
    def test_extracts_token_cookie_and_redacts_password(self):
        login_body = json.dumps({"acct": "person-a", "pwd": "secret-password"})
        login_response = json.dumps(
            {
                "code": 0,
                "data": {
                    "accessToken": "token-one",
                    "refreshToken": "refresh-one",
                    "pwd": "secret-password",
                },
            }
        )
        other = flow(
            "POST",
            "https://other.example/login",
            response_body=json.dumps({"data": {"accessToken": "do-not-keep"}}),
        )
        login = flow(
            "POST",
            "https://portal.example/api/login",
            request_body=login_body,
            response_body=login_response,
        )
        later = flow(
            "POST",
            "https://portal.example/api/price",
            request_headers=[["Cookie", "a=1; session=abc"], ["accessToken", "token-one"]],
            response_body=json.dumps({"code": 0, "data": {"rows": 1}}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flows.ndjson"
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in (other, login, later)) + "\n",
                encoding="utf-8",
            )
            result = proxy_edge.extract_flows(path, "portal.example")
            self.assertEqual(result["accessToken"], "token-one")
            self.assertEqual(result["refreshToken"], "refresh-one")
            self.assertEqual(result["cookie"], "a=1; session=abc")
            self.assertEqual(result["logins"][0]["request_fields"], ["acct", "pwd"])
            self.assertEqual(result["logins"][0]["path"], "/api/login")
            saved = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("secret-password", saved)
            self.assertIn("<redacted>", saved)
            self.assertNotIn("do-not-keep", saved)
            out = Path(tmp) / "auth.json"
            proxy_edge.write_auth(result, out)
            summary = proxy_edge.public_summary(result, out)
            self.assertEqual(summary["login_count"], 1)
            self.assertEqual(summary["access_token_len"], len("token-one"))
            self.assertNotIn("token-one", json.dumps(summary))

    def test_host_filter_ignores_suffix_lookalikes(self):
        rec = flow(
            "GET",
            "https://notportal.example/login",
            response_body=json.dumps({"accessToken": "nope"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flows.ndjson"
            path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            result = proxy_edge.extract_flows(path, "https://portal.example/app")
            self.assertEqual(result["matched_flows"], 0)
            self.assertIsNone(result["accessToken"])


class SafetyTests(unittest.TestCase):
    def test_edge_command_is_process_local(self):
        args = proxy_edge.edge_arguments(Path(r"D:\capture\edge_profile"), 8888, "https://portal.example/")
        joined = " ".join(args)
        self.assertIn("--proxy-server=http://127.0.0.1:8888", args)
        self.assertIn("--ignore-certificate-errors", args)
        self.assertIn("--user-data-dir=D:\\capture\\edge_profile", args)
        self.assertNotIn("certutil", joined.lower())
        self.assertNotIn("proxy-bypass", joined.lower())

    def test_sources_do_not_mutate_system_network(self):
        root = Path(__file__).resolve().parent
        text = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in (root / "proxy_edge.py", root / "live_dump.py")
        )
        for banned in (
            "certutil",
            "netsh",
            "set-itemproperty",
            "disable-netadapter",
            "new-netroute",
            "route add",
            "setvalueex",
            "winreg.setvalue",
        ):
            self.assertNotIn(banned, text)

    def test_running_proxy_reads_addon_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "live_dump.py"
            script.write_text('OUT = r"D:\\capture\\flows.ndjson"\n', encoding="utf-8")
            cmd = (
                r'"C:\Tools\mitmdump.exe" --listen-host 127.0.0.1 --listen-port 8888 '
                f'-s "{script}" -w D:\\capture\\flows.flow'
            )
            found = proxy_edge.running_proxy(
                [{"pid": 10, "name": "mitmdump.exe", "cmd": cmd}],
                8888,
            )
            self.assertEqual(found["pid"], 10)
            self.assertEqual(found["ndjson"], r"D:\capture\flows.ndjson")
            decoy = proxy_edge.running_proxy(
                [
                    {
                        "pid": 99,
                        "name": "DeepSeek Harness.exe",
                        "cmd": cmd + " mentioned by the agent transcript",
                    },
                    {"pid": 10, "name": "mitmdump.exe", "cmd": cmd},
                ],
                8888,
            )
            self.assertEqual(decoy["pid"], 10)
            edge = proxy_edge.edge_for_profile(
                [
                    {
                        "pid": 11,
                        "name": "msedge.exe",
                        "cmd": r'"C:\Edge\msedge.exe" --type=gpu-process --user-data-dir=D:\capture\edge_profile',
                    },
                    {
                        "pid": 12,
                        "name": "msedge.exe",
                        "cmd": r'"C:\Edge\msedge.exe" --proxy-server=http://127.0.0.1:8888 --user-data-dir=D:\capture\edge_profile https://portal.example/',
                    },
                ],
                Path(r"D:\capture\edge_profile"),
            )
            self.assertEqual(edge["pid"], 12)
            self.assertTrue(edge["running"])

    def test_system_proxy_read_is_stable(self):
        first = proxy_edge.read_system_proxy()
        second = proxy_edge.read_system_proxy()
        self.assertEqual(first, second)
        self.assertIn("proxy_enable", first)

    def test_session_conflict_does_not_replace_running_proxy(self):
        with self.assertRaises(proxy_edge.ToolError) as raised:
            proxy_edge.resolve_session(
                r"D:\new-session",
                {"session_dir": r"D:\current-session"},
            )
        self.assertEqual(raised.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
