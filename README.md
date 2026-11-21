# h2c-tunnel

A lightweight backup proxy tunnel based on **cleartext HTTP/2 (h2c) extended CONNECT** (RFC 8441).

A local HTTP/SOCKS5 proxy accepts browser traffic and forwards it over an h2c connection to a VPS, which connects to the target site on behalf of the client and relays bytes in both directions. Supports both HTTP and HTTPS (via CONNECT tunneling).

> **This project is a backup / experimental channel, not a high-performance replacement for production tools.** It is useful for protocol research and as a fallback when mainstream protocols are unavailable.

## How it works

```
Browser
   │  HTTP / SOCKS5 proxy (127.0.0.1:34132)
   ▼
h2c_proxy.py (local)
   │  HTTP/2 extended CONNECT over cleartext HTTP/2 (h2c)
   ▼
h2c_tunnel_server.py (on VPS, systemd service)
   │  connects to target, relays raw bytes
   ▼
Target website (HTTPS / HTTP)
```

## Features

- Cleartext HTTP/2 first hop (no TLS on the proxy-to-VPS leg)
- HTTP proxy and SOCKS5 on the same local port (`127.0.0.1:34132`)
- HTTP and HTTPS support (HTTPS via extended CONNECT tunneling)
- Connection pool: N h2c TCP connections, round-robin for bandwidth aggregation
- 16MB flow-control window, `TCP_NODELAY`, batched sends
- Clean shutdown on Ctrl+C / Ctrl+Z / Ctrl+Break, releasing the local port

## Repository layout

```
.
├── h2c_proxy.py               # Local HTTP + SOCKS5 proxy
├── config.json                # Local configuration (placeholder values)
├── h2c_client.py              # Standalone h2c CLI test client
├── run.bat / run.ps1          # Windows one-click launch scripts
├── server/
│   └── h2c_tunnel_server.py   # VPS-side tunnel server
├── LICENSE                    # MIT
├── README.md                  # English
└── README_zh.md               # 中文说明
```

## VPS server deployment

### 1. Requirements

- Python 3.10+
- Install dependency:
  ```bash
  python3 -m pip install --user --break-system-packages h2
  ```
- Pick unused ports. Defaults: IPv6 `2443`, IPv4 `8080`. **Do not use ports already taken by SSH or other services.**

### 2. Install the server

Upload `server/h2c_tunnel_server.py` to the VPS (e.g. `/home/<user>/h2c_tunnel_server.py`).

The server listens on:
- `[::]:2443` (IPv6)
- `0.0.0.0:8080` (IPv4)
- Extra IPv6 ports via the `H2C_EXTRA_PORTS` environment variable (comma-separated, optional)

### 3. Configure authentication

**Authentication is required.** The server rejects every connection unless a username and password are configured. The password is never sent in cleartext — clients prove knowledge of it with an HMAC-SHA256 signature over a random nonce, which also prevents replay attacks.

Create `/etc/default/h2c-tunnel` with your credentials:

```
H2C_AUTH_USER=your_username
H2C_AUTH_PASSWORD=your_strong_random_password
```

Restrict permissions: `sudo chmod 600 /etc/default/h2c-tunnel`.

Generate a strong password, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.

### 4. systemd service

Create `/etc/systemd/system/h2c-tunnel.service`:

```ini
[Unit]
Description=h2c tunnel server
After=network.target

[Service]
Type=simple
User=<your-user>
EnvironmentFile=/etc/default/h2c-tunnel
Environment=H2C_EXTRA_PORTS=43023
ExecStart=/usr/bin/python3 /home/<user>/h2c_tunnel_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `EnvironmentFile` loads the credentials from step 3 — do not omit it. `H2C_EXTRA_PORTS` is optional; remove that line if no extra port is needed.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now h2c-tunnel
systemctl is-active h2c-tunnel
```

Verify listeners:

```bash
ss -tulpn | grep -E ':2443|:8080|:43023'
```

### 5. Firewall

Open the chosen ports in your cloud provider's security group and the VPS firewall. Not every port is reachable on every network; if a port does not work, try a different high port and add it via `H2C_EXTRA_PORTS`.

## Local client deployment

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install h2     # Windows
# or
.venv/bin/python -m pip install h2             # Linux / macOS
```

On Windows you can simply run `run.bat` or `.\run.ps1`, which creates the venv and installs dependencies automatically.

If PyPI is not directly reachable, configure pip's index URL or proxy according to your own network environment.

### 2. Configure config.json

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

Fields:

| Field | Meaning |
|---|---|
| `local_host` / `local_port` | Local HTTP/SOCKS5 listen address |
| `mode` | `ipv6` or `ipv4`, which server address family to connect over |
| `ipv6.host` / `ipv4.host` | VPS IP address |
| `port` | Server h2c listen port |
| `authority` | Domain used in the HTTP/2 `:authority` header |
| `timeout` | Connection timeout in seconds |
| `h2c_connections` | Number of h2c TCP connections (higher values aggregate more bandwidth) |
| `auth_user` / `auth_password` | Credentials matching the server's `H2C_AUTH_USER` / `H2C_AUTH_PASSWORD` |

> The addresses and credentials above are placeholders. Replace them with your own VPS information and credentials.

### 3. Start

```bash
python h2c_proxy.py
```

You should see `h2c 隧道代理已启动：127.0.0.1:34132`. Press `Ctrl+C` / `Ctrl+Z` to stop and release the port.

### 4. Browser configuration

Set your browser (or proxy extension) to use `127.0.0.1:34132` as an HTTP or SOCKS5 proxy.

## Verification

Standalone test client:

```bash
python h2c_client.py <VPS_IP> <port> <authority>
```

Then open any HTTPS site through the configured proxy to confirm the tunnel works.

## Known limitations

- Implemented in Python over TCP. Throughput is limited by the language runtime and TCP congestion control; it is significantly slower than QUIC/Brutal-based tools (e.g. Hysteria2) or Go implementations.
- This is by design: the value of this project is protocol-mechanism verification and emergency backup, not raw speed.
- h2c is cleartext on the proxy-to-VPS leg; end-to-end security of target websites still relies on the browser-to-site TLS.

## Security notes

- Authentication is mandatory on the server; unauthenticated clients receive HTTP 403.
- Do not commit real VPS IPs, domains, credentials, or configuration to the repository.
- `config.json` contains placeholder values only; fill in your own details locally.

## License

MIT — see [LICENSE](LICENSE).

## Reference

- CircumVolve, CCS 2026 — https://gfw.report/publications/ccs26/zh/
- RFC 8441 — Bootstrapping WebSockets with HTTP/2 (extended CONNECT)
