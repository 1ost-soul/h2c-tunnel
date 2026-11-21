"""h2c 隧道代理：本地 SOCKS5/HTTP 代理 -> 共享 h2c 连接 -> VPS -> 目标网站。

运行后在本机 127.0.0.1:34132 开启代理，浏览器插件可直接连接。
配置文件：config.json（可修改域名、端口、IPv4/IPv6 模式等）。

优化版：
  - 连接池：维护多条到 VPS 的 h2c TCP 连接，每个浏览器 CONNECT 轮询分配一条，
    同一条连接内多个 CONNECT 复用为多个 HTTP/2 stream（多条 TCP 可叠加带宽）
  - 16MB 流控窗口（stream 级 + 连接级）+ 16MB max_frame_size
  - 收到数据后显式 acknowledge_received_data，及时归还流控窗口
  - 两端 socket 开启 TCP_NODELAY，减少小包延迟
  - 明文 HTTP 请求也走隧道，统一转发 HTTP/1.1 字节流
  - Ctrl+C / Ctrl+Z 干净退出，释放本地端口
"""

import hashlib
import hmac
import json
import os
import secrets
import select
import signal
import socket
import sys
import threading
from urllib.parse import urlparse

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded, StreamReset
from h2.settings import SettingCodes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

FLOW_WINDOW = 16 * 1024 * 1024
MAX_FRAME = 16777215

DEFAULT_CONFIG = {
    "local_host": "127.0.0.1",
    "local_port": 34132,
    "mode": "ipv6",
    "ipv6": {
        "host": "2001:db8::1",
        "port": 2443,
        "authority": "v6.example.com",
    },
    "ipv4": {
        "host": "192.0.2.1",
        "port": 8080,
        "authority": "example.com",
    },
    "timeout": 8,
    "h2c_connections": 4,
    "auth_user": "",
    "auth_password": "",
}

STOP_EVENT = threading.Event()


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"已生成默认配置：{CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def tune_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
    except OSError:
        pass


def tune_h2(conn):
    conn.initiate_connection()
    conn.update_settings(
        {
            SettingCodes.INITIAL_WINDOW_SIZE: FLOW_WINDOW,
            SettingCodes.MAX_FRAME_SIZE: MAX_FRAME,
            SettingCodes.ENABLE_CONNECT_PROTOCOL: 1,
        }
    )
    conn.increment_flow_control_window(FLOW_WINDOW - 65535, None)


def _status_of(headers):
    for k, v in headers:
        if k == ":status":
            return int(v)
    return 502


def _target_and_authority(cfg):
    mode = cfg.get("mode", "ipv6")
    section = cfg.get(mode, cfg["ipv6"])
    return section, section.get("authority", section["host"])


def parse_authority(authority):
    if not authority:
        return None, None
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None, None
        host = authority[1:end]
        rest = authority[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else 443
        return host, port
    if ":" in authority:
        host, port = authority.rsplit(":", 1)
        return host, int(port)
    return authority, 443


class Stream:
    """一条 h2c 隧道 stream 的状态。"""

    def __init__(self, stream_id):
        self.stream_id = stream_id
        self.status = None
        self.ready = threading.Event()
        self.buf = bytearray()
        self.eof = False
        self.lock = threading.Lock()
        self.notify_r, self.notify_w = socket.socketpair()

    def push(self, data):
        with self.lock:
            was_empty = not self.buf
            self.buf.extend(data)
        if was_empty:
            self._notify()

    def finish(self):
        with self.lock:
            self.eof = True
        self._notify()

    def _notify(self):
        try:
            self.notify_w.send(b"x")
        except OSError:
            pass

    def drain(self):
        with self.lock:
            data = bytes(self.buf)
            self.buf.clear()
            eof = self.eof
        return data, eof

    def close(self):
        for s in (self.notify_r, self.notify_w):
            try:
                s.close()
            except OSError:
                pass


class H2Client:
    """维护一条到 VPS 的共享 h2c 连接，支持多个并发隧道 stream。"""

    def __init__(self, cfg, target):
        self.cfg = cfg
        self.target = target
        self.lock = threading.RLock()
        self.sock = None
        self.conn = None
        self.streams = {}
        self.next_stream = 1
        self.running = False
        self.error = None
        self.recv_thread = None

    def _connect(self):
        host = self.target["host"]
        port = int(self.target["port"])
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(self.cfg.get("timeout", 8))
        s.connect((host, port))
        tune_socket(s)
        s.settimeout(None)

        conn = H2Connection(config=H2Configuration(header_encoding="utf-8"))
        tune_h2(conn)
        s.sendall(conn.data_to_send())

        self.sock = s
        self.conn = conn
        self.running = True
        self.error = None
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

    def _flush_locked(self):
        data = self.conn.data_to_send()
        if data:
            self.sock.sendall(data)

    def _recv_loop(self):
        try:
            while self.running:
                data = self.sock.recv(65535)
                if not data:
                    raise EOFError("h2c connection closed by peer")
                with self.lock:
                    events = self.conn.receive_data(data)
                    for event in events:
                        if isinstance(event, DataReceived):
                            try:
                                self.conn.acknowledge_received_data(
                                    getattr(event, "flow_controlled_length", len(event.data)),
                                    event.stream_id,
                                )
                            except Exception:
                                pass
                    self._flush_locked()
                for event in events:
                    self._dispatch(event)
        except Exception as e:
            self._mark_error(e)

    def _dispatch(self, event):
        if not isinstance(event, (ResponseReceived, DataReceived, StreamEnded, StreamReset)):
            return
        with self.lock:
            stream = self.streams.get(event.stream_id)
        if stream is None:
            return
        if isinstance(event, ResponseReceived):
            stream.status = _status_of(event.headers)
            stream.ready.set()
        elif isinstance(event, DataReceived):
            stream.push(event.data)
        elif isinstance(event, (StreamEnded, StreamReset)):
            stream.finish()
            stream.ready.set()

    def _mark_error(self, error):
        print(f"[h2c-error] {type(error).__name__}: {error}", flush=True)
        with self.lock:
            self.running = False
            self.error = error
            streams = list(self.streams.values())
            for st in streams:
                st.finish()
                st.ready.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _auth_headers(self):
        """生成 HMAC 鉴权头：密码不明文传输，随机 nonce 防重放（不依赖时钟）。"""
        cfg = self.cfg
        user = cfg.get("auth_user", "")
        password = cfg.get("auth_password", "")
        if not user or not password:
            return []
        nonce = secrets.token_hex(24)
        msg = f"{user}|{nonce}".encode("utf-8")
        sig = hmac.new(password.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return [
            ("x-auth-user", user),
            ("x-auth-nonce", nonce),
            ("x-auth-sig", sig),
        ]

    def open(self, host, port, timeout):
        """打开一个新隧道 stream，返回已收到 200 的 Stream。"""
        with self.lock:
            if not self.running:
                if self.error:
                    self.streams.clear()
                    self.next_stream = 1
                self._connect()
            sid = self.next_stream
            self.next_stream += 2
            stream = Stream(sid)
            self.streams[sid] = stream
            headers = [
                (":method", "CONNECT"),
                (":protocol", "h2c-tunnel"),
                (":scheme", "http"),
                (":path", "/"),
                (":authority", f"{host}:{port}"),
                ("x-target-host", host),
                ("x-target-port", str(port)),
                ("user-agent", "h2c-tunnel-client/1.1"),
            ]
            headers.extend(self._auth_headers())
            self.conn.send_headers(sid, headers, end_stream=False)
            self._flush_locked()

        if not stream.ready.wait(timeout):
            raise TimeoutError("tunnel response timeout")
        if stream.status != 200:
            raise RuntimeError(f"upstream refused tunnel: {stream.status}")
        return stream

    def send(self, stream_id, data):
        with self.lock:
            if not self.running:
                raise EOFError("h2c connection down")
            self.conn.send_data(stream_id, data, end_stream=False)
            self._flush_locked()

    def close_stream(self, stream_id):
        with self.lock:
            stream = self.streams.pop(stream_id, None)
            if stream and self.running:
                try:
                    self.conn.end_stream(stream_id)
                except Exception:
                    pass
                try:
                    self._flush_locked()
                except Exception:
                    pass
            if stream:
                stream.close()


def relay_tunnel(browser, cfg, host, port, http_reply=True, initial_data=None):
    """打开 h2c 隧道并在浏览器与隧道之间双向转发原始字节。"""
    target, _authority = _target_and_authority(cfg)
    client = _get_client(cfg, target)
    try:
        stream = client.open(host, port, cfg.get("timeout", 8))
    except Exception as e:
        print(f"[tunnel-error] {type(e).__name__}: {e}", flush=True)
        msg = f"h2c tunnel error: {type(e).__name__}: {e}".encode()
        if http_reply:
            browser.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: "
                + str(len(msg)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + msg
            )
        browser.close()
        return

    if http_reply:
        browser.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    elif initial_data:
        try:
            client.send(stream.stream_id, initial_data)
        except Exception:
            pass

    notify_r = stream.notify_r
    try:
        while True:
            readable, _, _ = select.select([browser, notify_r], [], [], 300)
            if not readable:
                break
            if browser in readable:
                data = browser.recv(65535)
                if not data:
                    break
                client.send(stream.stream_id, data)
            if notify_r in readable:
                try:
                    notify_r.recv(4096)
                except OSError:
                    pass
                data, eof = stream.drain()
                if data:
                    browser.sendall(data)
                if eof:
                    break
    except Exception as e:
        print(f"[tunnel] {type(e).__name__}: {e}", flush=True)
    finally:
        client.close_stream(stream.stream_id)
        try:
            browser.close()
        except OSError:
            pass


_client_lock = threading.Lock()
_clients = {}


def _get_client(cfg, target):
    """按 (模式, host, port) 取一个 H2Client，多条 TCP 轮询分配以叠加带宽。"""
    key = (cfg.get("mode"), str(target["host"]), int(target["port"]))
    n = max(1, int(cfg.get("h2c_connections", 4)))
    with _client_lock:
        entry = _clients.get(key)
        if entry is None:
            entry = {"clients": [], "index": 0}
            _clients[key] = entry
        clients = entry["clients"]
        if len(clients) < n:
            clients.append(H2Client(cfg, target))
        client = clients[entry["index"] % len(clients)]
        entry["index"] += 1
        return client


def _read_http_request(conn):
    """读取一个 HTTP 请求，返回 (method, raw_url, headers, raw_bytes)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(65535)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 1024 * 1024:
            return None
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("utf-8", "replace").split("\r\n")
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    method, raw_url = parts[0], parts[1]
    headers = []
    content_length = 0
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        headers.append((k, v))
        if k.lower() == "content-length":
            try:
                content_length = int(v)
            except ValueError:
                content_length = 0
    body = rest
    while len(body) < content_length:
        chunk = conn.recv(65535)
        if not chunk:
            break
        body += chunk
    raw = head + b"\r\n\r\n" + body[:content_length]
    return method, raw_url, headers, raw


def handle_http_proxy(conn, cfg):
    req = _read_http_request(conn)
    if not req:
        conn.close()
        return
    method, raw_url, headers, raw = req
    if method == "CONNECT":
        target_host, target_port = (
            raw_url.rsplit(":", 1) if ":" in raw_url else (raw_url, "443")
        )
        relay_tunnel(conn, cfg, target_host, int(target_port), http_reply=True)
        return

    # 明文 HTTP：解析目标，把原始 HTTP/1.1 请求字节塞进 h2c 隧道
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        u = urlparse(raw_url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
    else:
        host_header = next((v for k, v in headers if k.lower() == "host"), None)
        host, port = parse_authority(host_header)
        if not port:
            port = 80
    if not host:
        conn.close()
        return
    relay_tunnel(conn, cfg, host, int(port), http_reply=False, initial_data=raw)


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError("socks5 peer closed")
        data += chunk
    return data


def handle_socks5(conn, cfg):
    """最小 SOCKS5 实现：无认证，CONNECT 后双向转发原始字节。"""
    head = recv_exact(conn, 2)
    if head[0] != 0x05:
        conn.close()
        return
    nmethods = head[1]
    recv_exact(conn, nmethods)
    conn.sendall(b"\x05\x00")

    req = recv_exact(conn, 4)
    if req[1] != 0x01:
        conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
        conn.close()
        return

    atyp = req[3]
    if atyp == 0x01:
        addr = recv_exact(conn, 4)
    elif atyp == 0x03:
        n = recv_exact(conn, 1)[0]
        addr = recv_exact(conn, n)
    elif atyp == 0x04:
        addr = recv_exact(conn, 16)
    else:
        conn.close()
        return
    port = int.from_bytes(recv_exact(conn, 2), "big")
    conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

    if atyp == 0x01:
        host = socket.inet_ntoa(addr)
    elif atyp == 0x03:
        host = addr.decode("utf-8", "replace")
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, addr)
    else:
        conn.close()
        return
    relay_tunnel(conn, cfg, host, port, http_reply=False)


def handle_client(conn, cfg):
    try:
        tune_socket(conn)
        first = conn.recv(1, socket.MSG_PEEK)
        if not first:
            conn.close()
        elif first == b"\x05":
            handle_socks5(conn, cfg)
        else:
            handle_http_proxy(conn, cfg)
    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}", flush=True)
        try:
            conn.close()
        except OSError:
            pass


def _stdin_watcher():
    """监听 Ctrl+Z（^Z 字符）触发干净退出；后台 stdin 的 EOF 只退出监听线程。"""
    try:
        while True:
            ch = sys.stdin.read(1)
            if ch == "":
                # 后台/非交互 stdin 的 EOF：停止监听，但不影响代理运行
                return
            if ch == "\x1a":
                STOP_EVENT.set()
                return
    except Exception:
        pass


def _signal_handler(signum, frame):
    STOP_EVENT.set()


def main():
    cfg = load_config()
    host = cfg.get("local_host", "127.0.0.1")
    port = int(cfg.get("local_port", 34132))
    target, authority = _target_and_authority(cfg)
    n = max(1, int(cfg.get("h2c_connections", 4)))
    print(f"h2c 隧道代理已启动：{host}:{port}", flush=True)
    print(
        f"模式={cfg.get('mode')} -> {target['host']}:{target['port']} authority={authority}",
        flush=True,
    )
    print(
        f"流控窗口={FLOW_WINDOW // (1024 * 1024)}MB  TCP_NODELAY=on  连接复用=on  h2c连接数={n}",
        flush=True,
    )
    print(f"配置文件：{CONFIG_PATH}", flush=True)
    print("按 Ctrl+C 或 Ctrl+Z 停止并释放端口", flush=True)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass
    # 仅在真实交互终端启用 Ctrl+Z(stdin EOF) 监听；后台运行时 stdin 会立即 EOF，避免误退出
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=_stdin_watcher, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    srv.settimeout(0.5)
    try:
        while not STOP_EVENT.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle_client, args=(conn, cfg), daemon=True).start()
    except KeyboardInterrupt:
        STOP_EVENT.set()
    finally:
        srv.close()
        with _client_lock:
            for entry in _clients.values():
                for client in entry["clients"]:
                    if client.sock:
                        try:
                            client.sock.close()
                        except OSError:
                            pass
        print("\n[h2c] 已停止，端口已释放", flush=True)


if __name__ == "__main__":
    main()
