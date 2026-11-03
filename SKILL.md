---
name: local-proxy-edge
description: 在本机 127.0.0.1 的 mitmproxy 上打开独立 Edge，让用户自己登录并抓包；按主机名把登录接口、accessToken 和 Cookie 写入本地 auth.json。Edge 退出后可按同一配置重新打开。不修改系统代理、证书库、网卡或路由。只用于操作者本人已授权的本机登录态抓取，不用于他人会话或系统级劫持。
---

# 本地代理 Edge

给用户开一个只走 `127.0.0.1:8888` 的独立 Edge 窗口。用户自己登录。流量追加进该会话的 `flows.ndjson`。登录完成后按主机名提取 accessToken、refreshToken 和 Cookie。

不改系统代理、Windows 证书库、网卡、路由或现有 Clash 类代理。证书错误只在这个独立窗口上忽略，不把 mitmproxy CA 装进系统证书库。

## 何时使用

用户要打开代理浏览器、继续登录、Edge 掉了要重开、或从已有 `flows.ndjson` 里按站点提取登录态。没有目标网址时先问，不要猜。

## 命令

在本 skill 目录执行。Python 用 3.11 即可，提取不依赖 mitmproxy；只有代理没在监听时才需要本机的 `mitmdump`。

```powershell
python scripts/proxy_edge.py status
python scripts/proxy_edge.py open --url "https://目标站点/"
python scripts/proxy_edge.py relaunch --url "https://目标站点/"
python scripts/proxy_edge.py relaunch --url "https://目标站点/" --force
python scripts/proxy_edge.py extract --host "目标主机名"
```

`status`、`open` 和 `relaunch` 都先看 `127.0.0.1:8888`。已经是 mitmdump 就复用，不重启、不换落盘文件。端口被别的程序占用就停下并说明，不结束那个进程。

`open` 使用单独的 `--user-data-dir`。同配置的窗口还在时不要再开一个。用户说窗口关了、崩溃了或要继续时，先 `status`；主进程不在就 `relaunch`。只有用户明确说窗口卡住、关掉重开时才加 `--force`。`--force` 只结束命令行里带这个 user-data-dir 的 Edge，不结束用户平时的 Edge。

代理进程也不在时，`open` / `relaunch` 会在同一会话目录重新拉起 mitmdump，NDJSON 和 flow 文件都追加，不截断。新的代理仍只绑定 `127.0.0.1`。

`extract` 流式读取 NDJSON，主机名精确匹配。结果写到 `<会话>\auth\<主机名>\auth.json`，可用 `--ndjson` 和 `--out` 改位置。标准输出只有路径、接口、条数和令牌长度，没有令牌或 Cookie 值。密码、验证码字段不会写进 auth.json。

## 交给用户

打开窗口后告诉用户：请在这个新窗口登录，登录好了回一声。不要替用户输入账号、密码、短信验证码或扫码。不要同时用另一路客户端挤掉这个会话。

提取后报告主机、登录接口、accessToken/refreshToken/Cookie 的长度，以及 auth.json 路径。不要把令牌、Cookie、验证码或 auth.json 内容贴进对话、提交或外发。

## 边界

- 一个本机代理只写一个 NDJSON。另一个会话目录不会自动变成新的落盘位置。
- auth.json、flows.ndjson、flow 文件、Edge 配置目录和 mitmproxy CA 只留本机。
- 不读取或复用用户日常浏览器配置。
- 目标站点若还要签名、实名或参保状态，提取到令牌不等于业务接口已经可用。
