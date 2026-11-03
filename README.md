# local-proxy-edge

本机独立 Edge 走本地 mitmproxy 抓取操作者自己的登录态。安装到 Codex / DeepSeek Harness 的 skills 目录后，其他 agent 用同一组命令打开窗口、续抓和提取令牌。

不修改系统代理、Windows 证书库、网卡或路由。

## 设备

2026-11-03 在这台电脑上核对过运行环境：

| 项目 | 值 |
|---|---|
| 系统 | Windows 10 Pro 22H2，build 19045.6466 |
| 浏览器 | Microsoft Edge 156.0.4314.58 |
| Edge 路径 | `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` |
| 代理 | mitmproxy 11.0.2 的 `mitmdump`，只监听 `127.0.0.1:8888` |
| Python | 3.11.9（64 位） |

这台电脑原来就有用户级代理。本工具只读取它来确认自己没有改动，独立 Edge 另用进程参数 `--proxy-server=http://127.0.0.1:8888`，不继承、不回写系统代理。

## 实现

1. `mitmdump` 加载 `scripts/live_dump.py`。每条 HTTP 流结束就追加一行 JSON 到会话目录的 `flows.ndjson`，包含请求/响应头和体。进程被结束时最多丢掉尚未写完的一条。
2. 用 PowerShell `Start-Process` 打开独立 Edge。参数是 `--proxy-server=http://127.0.0.1:8888`、单独的 `--user-data-dir`、`--ignore-certificate-errors`，以及关闭首次运行、默认为浏览器提示、同步和后台联网。证书告警只在这个窗口忽略，不调用 `certutil`，不改系统证书库。
3. 人在这个窗口登录。窗口还在就复用；窗口没了就按同一配置重新打开。代理没了也只在 `127.0.0.1:8888` 重新监听，并追加原流量文件，不截断。
4. `extract` 按主机名精确筛选 NDJSON，把登录接口、accessToken、refreshToken 和 Cookie 写到本地 `auth.json`。命令输出只含路径和长度。密码、验证码字段不保存。

流量文件默认在当前目录的 `capture_live_年月日_时分`。已有 mitmdump 在监听时继续写它原来的文件，不会另开一套目录假装正在抓包。

## 安装

把本目录放到 agent 的 skills 目录，目录名保持 `local-proxy-edge`：

```text
%USERPROFILE%\.codex\skills\local-proxy-edge\SKILL.md
```

DeepSeek Harness 若已把该 skills 目录加入扫描路径，新会话即可调用。还需要：

- Windows 上的 Microsoft Edge
- 监听 `127.0.0.1:8888` 的 mitmproxy 11；不在 PATH 上时用 `--mitmdump` 指定 `mitmdump.exe`
- Python 3.11，用于 `scripts/proxy_edge.py`

提取登录态不需要安装 mitmproxy。只有本机 8888 没有代理时才会启动它。

## 用法

```powershell
python scripts/proxy_edge.py status
python scripts/proxy_edge.py open --url "https://目标站点/"
python scripts/proxy_edge.py relaunch --url "https://目标站点/"
python scripts/proxy_edge.py extract --host "目标主机名"
```

`auth.json`、`flows.ndjson`、Edge 用户目录和 mitmproxy 的 CA 私钥都不要提交或外发。

## 验证

不访问真实业务站点，也不读取既有抓包：

```powershell
python scripts/test_proxy_edge.py
```

覆盖主机名筛选、令牌与 Cookie 提取、密码字段不落盘、Edge 参数不包含系统证书或路由命令，以及已有代理会话不会被另一个目录替换。命令行 `extract` 另用临时文件核对过：标准输出只有接口和令牌长度。

2026-11-03 在上面这台电脑上又做了只读核对：`status` 认出了已经监听 `127.0.0.1:8888` 的 mitmdump 和它对应的独立 Edge；`open --dry-run` 发现窗口还在，没有再开一个，也没有重启代理。核对前后，用户级代理注册表、8888 监听进程和该 Edge 进程都没有变化。
