# h2c-tunnel

基于**明文 HTTP/2（h2c）extended CONNECT**（RFC 8441）实现的轻量级备用代理隧道。

本地 HTTP/SOCKS5 代理接收浏览器流量，通过 h2c 连接转发到 VPS，由 VPS 代替客户端连接目标网站并双向转发原始字节。支持 HTTP 与 HTTPS（通过 CONNECT 隧道）。

> **本项目是备用 / 实验性通道，不是高性能主力工具。** 适合协议研究与主力协议不可用时的应急备份。

## 工作原理

```
浏览器
   │  HTTP / SOCKS5 代理（127.0.0.1:34132）
   ▼
h2c_proxy.py（本地）
   │  明文 HTTP/2 extended CONNECT（h2c）
   ▼
h2c_tunnel_server.py（VPS，systemd 服务）
   │  代替客户端连接目标，双向转发原始字节
   ▼
目标网站（HTTPS / HTTP）
```

## 特性

- 第一跳为明文 HTTP/2（代理到 VPS 段不使用 TLS）
- 同一本地端口同时支持 HTTP 代理与 SOCKS5（`127.0.0.1:34132`）
- 支持 HTTP 与 HTTPS（HTTPS 走 extended CONNECT 隧道）
- 连接池：N 条 h2c TCP 轮询分配，可叠加带宽
- 16MB 流控窗口、`TCP_NODELAY`、批量发送
- Ctrl+C / Ctrl+Z / Ctrl+Break 干净退出并释放端口

## 目录结构

```
.
├── h2c_proxy.py               # 本地 HTTP + SOCKS5 代理
├── config.json                # 本地配置（占位值）
├── h2c_client.py              # 独立 h2c 命令行测试客户端
├── run.bat / run.ps1          # Windows 一键启动脚本
├── server/
│   └── h2c_tunnel_server.py   # VPS 端隧道服务端
├── LICENSE                    # MIT
├── README.md                  # 英文说明
└── README_zh.md               # 中文说明
```

## VPS 服务端部署

### 1. 环境要求

- Python 3.10+
- 安装依赖：
  ```bash
  python3 -m pip install --user --break-system-packages h2
  ```
- 选择未被占用的端口（默认 IPv6 `2443`、IPv4 `8080`）。**不要使用 SSH 或其他服务已占用的端口。**

### 2. 安装服务端

把 `server/h2c_tunnel_server.py` 上传到 VPS（例如 `/home/<user>/h2c_tunnel_server.py`）。

服务端默认监听：
- `[::]:2443`（IPv6）
- `0.0.0.0:8080`（IPv4）
- 额外 IPv6 端口由环境变量 `H2C_EXTRA_PORTS` 指定（逗号分隔，可选）

### 3. 配置鉴权

**鉴权是强制的。** 未配置用户名和密码时，服务端拒绝一切连接。密码不会明文传输——客户端用随机 nonce 的 HMAC-SHA256 签名来证明持有密码，同时防止重放攻击。

创建 `/etc/default/h2c-tunnel`，写入你的凭据：

```
H2C_AUTH_USER=your_username
H2C_AUTH_PASSWORD=your_strong_random_password
```

设置权限：`sudo chmod 600 /etc/default/h2c-tunnel`。

生成强密码示例：`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`。

### 4. 配置 systemd

创建 `/etc/systemd/system/h2c-tunnel.service`：

```ini
[Unit]
Description=h2c tunnel server
After=network.target

[Service]
Type=simple
User=<你的用户名>
EnvironmentFile=/etc/default/h2c-tunnel
Environment=H2C_EXTRA_PORTS=43023
ExecStart=/usr/bin/python3 /home/<user>/h2c_tunnel_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `EnvironmentFile` 用于加载第 3 步的凭据，不可省略。`H2C_EXTRA_PORTS` 为可选项，不需要额外端口就删除这一行。

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now h2c-tunnel
systemctl is-active h2c-tunnel
```

确认监听：

```bash
ss -tulpn | grep -E ':2443|:8080|:43023'
```

### 5. 防火墙

在云厂商安全组和 VPS 防火墙放行所选端口。不同网络环境下并非所有端口都可达；若某端口不通，换一个高位端口并通过 `H2C_EXTRA_PORTS` 监听。

## 本地客户端部署

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install h2     # Windows
# 或
.venv/bin/python -m pip install h2             # Linux / macOS
```

Windows 用户可直接运行 `run.bat` 或 `.\run.ps1`，脚本会自动创建虚拟环境并安装依赖。

如果 PyPI 无法直连，请根据自身网络环境配置 pip 的镜像源或代理。

### 2. 修改 config.json

```json
{
  "local_host": "127.0.0.1",
  "local_port": 34132,
  "mode": "ipv6",
  "ipv6": {
    "host": "2001:db8::1",
    "port": 2443,
    "authority": "v6.example.com"
  },
  "ipv4": {
    "host": "192.0.2.1",
    "port": 8080,
    "authority": "example.com"
  },
  "timeout": 8,
  "h2c_connections": 16,
  "auth_user": "your_username",
  "auth_password": "your_strong_random_password"
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `local_host` / `local_port` | 本地 HTTP/SOCKS5 监听地址 |
| `mode` | `ipv6` 或 `ipv4`，连接服务端使用的地址族 |
| `ipv6.host` / `ipv4.host` | VPS 的 IP 地址 |
| `port` | 服务端 h2c 监听端口 |
| `authority` | HTTP/2 `:authority` 头使用的域名 |
| `timeout` | 连接超时（秒） |
| `h2c_connections` | h2c TCP 连接条数，越大越能叠加带宽 |
| `auth_user` / `auth_password` | 与服务端 `H2C_AUTH_USER` / `H2C_AUTH_PASSWORD` 一致的凭据 |

> 以上地址和凭据均为占位值，请替换为你自己的 VPS 信息和凭据。

### 3. 启动

```bash
python h2c_proxy.py
```

看到 `h2c 隧道代理已启动：127.0.0.1:34132` 即可。按 `Ctrl+C` / `Ctrl+Z` 停止并释放端口。

### 4. 浏览器配置

把浏览器（或代理插件）的代理设置为 `127.0.0.1:34132`，HTTP 或 SOCKS5 均可。

## 验证

独立测试客户端：

```bash
python h2c_client.py <VPS_IP> <port> <authority>
```

然后通过配置好的代理访问任意 HTTPS 网站，确认隧道可用。

## 已知局限

- 使用 Python 实现并基于 TCP，吞吐受语言运行时和 TCP 拥塞控制限制，明显低于基于 QUIC/Brutal 的工具（如 Hysteria2）或 Go 实现。
- 这是设计取舍：本项目的价值在于协议机制验证与应急备份，而非速度。
- h2c 在代理到 VPS 段为明文；目标网站的安全性仍由浏览器到站点的 TLS 保障。

## 安全提示

- 服务端强制鉴权，未鉴权的客户端会被返回 HTTP 403。
- 请勿将真实的 VPS IP、域名、凭据或配置提交到仓库。
- `config.json` 仅含占位值，请在本地填写自己的信息。

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

## 参考

- CircumVolve, CCS 2026 — https://gfw.report/publications/ccs26/zh/
- RFC 8441 — Bootstrapping WebSockets with HTTP/2（extended CONNECT）
