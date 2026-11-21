"""VPS 端 h2c 隧道服务端（优化版，支持多 stream + 大流控窗口）。

收到客户端的 h2c CONNECT 请求后，代替客户端连接目标网站，
并在 HTTP/2 stream 上双向转发原始字节（TLS 等）。

优化点：
  - 一条 h2c 连接承载多个 stream（每个浏览器 CONNECT 一个 stream）
  - HTTP/2 初始窗口 16MB + 连接级窗口 16MB + max_frame_size 16MB
  - 收到数据后显式 acknowledge_received_data，及时归还流控窗口
  - 两端 socket 开启 TCP_NODELAY，减少小包延迟

监听：
  [::]:2443      （IPv6，当前唯一可达低端口）
  0.0.0.0:8080   （IPv4，配置保留，当前审查下不可达）
  [::]:<额外端口>（由环境变量 H2C_EXTRA_PORTS 指定，逗号分隔，用于随机高位端口测试）

依赖：pip install h2
"""

import hashlib
import hmac
import os
import select
import socket
import threading

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded, StreamReset
from h2.settings import SettingCodes

FLOW_WINDOW = 16 * 1024 * 1024
MAX_FRAME = 16777215


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


# 鉴权配置（通过环境变量设置；未设置则拒绝所有连接）
AUTH_USER = os.environ.get("H2C_AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("H2C_AUTH_PASSWORD", "")

# 已使用的 nonce 集合，防止重放（不依赖时钟，永久记录）
_used_nonces = set()
_nonce_lock = threading.Lock()
_MAX_NONCES = 100000  # 超过此数量时清空，避免无限增长


def check_auth(headers):
    """校验 HMAC 鉴权头：用户名匹配 + 签名正确 + nonce 未用过。"""
    if not AUTH_USER or not AUTH_PASSWORD:
        print("[server] WARNING: no auth configured (H2C_AUTH_USER/PASSWORD), rejecting all", flush=True)
        return False
    user = headers.get("x-auth-user", "")
    nonce = headers.get("x-auth-nonce", "")
    sig = headers.get("x-auth-sig", "")
    if not (user and nonce and sig):
        return False
    if not hmac.compare_digest(user, AUTH_USER):
        return False
    msg = f"{user}|{nonce}".encode("utf-8")
    expected = hmac.new(AUTH_PASSWORD.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    with _nonce_lock:
        if nonce in _used_nonces:
            return False
        if len(_used_nonces) >= _MAX_NONCES:
            _used_nonces.clear()
        _used_nonces.add(nonce)
    return True


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


def handle_client(client_sock, client_addr):
    tune_socket(client_sock)
    conn = H2Connection(config=H2Configuration(header_encoding="utf-8", client_side=False))
    tune_h2(conn)
    client_sock.sendall(conn.data_to_send())

    tunnels = {}
    lock = threading.RLock()

    def flush_locked():
        data = conn.data_to_send()
        if data:
            client_sock.sendall(data)

    def pump_target(target_sock, stream_id):
        pending = bytearray()
        try:
            while True:
                data = target_sock.recv(65535)
                if not data:
                    break
                pending.extend(data)
                more, _, _ = select.select([target_sock], [], [], 0)
                if len(pending) >= 32768 or not more:
                    with lock:
                        conn.send_data(stream_id, bytes(pending), end_stream=False)
                        flush_locked()
                    pending.clear()
            if pending:
                with lock:
                    conn.send_data(stream_id, bytes(pending), end_stream=False)
                    flush_locked()
        except Exception:
            pass
        finally:
            with lock:
                try:
                    conn.end_stream(stream_id)
                except Exception:
                    pass
                flush_locked()
                tunnels.pop(stream_id, None)
            try:
                target_sock.close()
            except OSError:
                pass

    try:
        while True:
            data = client_sock.recv(65535)
            if not data:
                break
            with lock:
                events = conn.receive_data(data)
                for event in events:
                    if isinstance(event, DataReceived):
                        try:
                            conn.acknowledge_received_data(
                                getattr(event, "flow_controlled_length", len(event.data)),
                                event.stream_id,
                            )
                        except Exception:
                            pass
                flush_locked()

            for event in events:
                if isinstance(event, RequestReceived):
                    headers = {}
                    for k, v in event.headers:
                        if isinstance(k, bytes):
                            k = k.decode("utf-8", "replace")
                        if isinstance(v, bytes):
                            v = v.decode("utf-8", "replace")
                        headers[k] = v

                    method = headers.get(":method", "").upper()
                    if method != "CONNECT":
                        with lock:
                            conn.send_headers(event.stream_id, [(":status", "405")], end_stream=True)
                            flush_locked()
                        continue

                    if not check_auth(headers):
                        with lock:
                            conn.send_headers(event.stream_id, [(":status", "403")], end_stream=True)
                            flush_locked()
                        continue

                    host = headers.get("x-target-host")
                    port = headers.get("x-target-port")
                    if not host:
                        host, parsed_port = parse_authority(headers.get(":authority"))
                        if not port and parsed_port:
                            port = parsed_port
                    if not host:
                        with lock:
                            conn.send_headers(event.stream_id, [(":status", "400")], end_stream=True)
                            flush_locked()
                        continue

                    try:
                        target = socket.create_connection((host, int(port)), timeout=12)
                        tune_socket(target)
                    except Exception:
                        with lock:
                            conn.send_headers(event.stream_id, [(":status", "502")], end_stream=True)
                            flush_locked()
                        continue

                    with lock:
                        tunnels[event.stream_id] = target
                        conn.send_headers(event.stream_id, [(":status", "200")], end_stream=False)
                        flush_locked()
                    threading.Thread(
                        target=pump_target,
                        args=(target, event.stream_id),
                        daemon=True,
                    ).start()

                elif isinstance(event, DataReceived):
                    with lock:
                        target = tunnels.get(event.stream_id)
                    if target:
                        try:
                            target.sendall(event.data)
                        except OSError:
                            with lock:
                                tunnels.pop(event.stream_id, None)
                            try:
                                target.close()
                            except OSError:
                                pass

                elif isinstance(event, (StreamEnded, StreamReset)):
                    with lock:
                        target = tunnels.pop(event.stream_id, None)
                    if target:
                        try:
                            target.close()
                        except OSError:
                            pass
    except Exception as e:
        print(f"[server] {client_addr} error: {type(e).__name__}: {e}", flush=True)
    finally:
        with lock:
            targets = list(tunnels.values())
            tunnels.clear()
        for t in targets:
            try:
                t.close()
            except OSError:
                pass
        try:
            client_sock.close()
        except OSError:
            pass


def serve(family, bind_addr, port):
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    srv.bind((bind_addr, port))
    srv.listen(128)
    label = "IPv6" if family == socket.AF_INET6 else "IPv4"
    print(f"[server] h2c tunnel listening on {label} {bind_addr}:{port}", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        tune_socket(conn)
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


def main():
    extra_ports = []
    for item in os.environ.get("H2C_EXTRA_PORTS", "").split(","):
        item = item.strip()
        if item.isdigit():
            extra_ports.append(int(item))

    threads = [
        threading.Thread(target=serve, args=(socket.AF_INET6, "::", 2443), daemon=True),
        threading.Thread(target=serve, args=(socket.AF_INET, "0.0.0.0", 8080), daemon=True),
    ]
    for p in extra_ports:
        threads.append(
            threading.Thread(target=serve, args=(socket.AF_INET6, "::", p), daemon=True)
        )
    for t in threads:
        t.start()
    print("[server] h2c tunnel server started", flush=True)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
