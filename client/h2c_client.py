"""轻量级 h2c（明文 HTTP/2）客户端原型。

用法：
  python h2c_client.py <host> <port> <authority>

示例：
  python h2c_client.py 2001:db8::1 2443 v6.example.com

依赖：
  pip install h2
"""

import socket
import sys

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded


def probe(host, port, authority, timeout=8):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))

    # h2 库的 initiate_connection() 会自动发送客户端 preface 与 SETTINGS，
    # 不要手动再发一次 PRI * HTTP/2.0，否则服务器会收到两份 preface。
    conn = H2Connection(config=H2Configuration(header_encoding="utf-8"))
    conn.initiate_connection()
    s.sendall(conn.data_to_send())

    conn.send_headers(
        1,
        [
            (":method", "GET"),
            (":scheme", "http"),
            (":path", "/"),
            (":authority", authority),
            ("user-agent", "h2c-probe/1.0"),
        ],
        end_stream=True,
    )
    s.sendall(conn.data_to_send())

    body = b""
    resp_headers = []
    while True:
        try:
            data = s.recv(65535)
        except socket.timeout:
            break
        if not data:
            break
        for event in conn.receive_data(data):
            if isinstance(event, ResponseReceived):
                resp_headers = event.headers
            elif isinstance(event, DataReceived):
                body += event.data
            elif isinstance(event, StreamEnded):
                s.close()
                return resp_headers, body
        s.sendall(conn.data_to_send())

    s.close()
    return None, None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "2001:db8::1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2443
    authority = sys.argv[3] if len(sys.argv) > 3 else "v6.example.com"

    try:
        headers, body = probe(host, port, authority)
    except Exception as e:
        print(f"h2c FAILED {host}:{port} - {type(e).__name__}: {e}")
        return

    if headers is None:
        print(f"h2c FAILED {host}:{port} - no response")
        return

    print(f"h2c OK {host}:{port}")
    for k, v in headers:
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        print(f"  {k}: {v}")
    print("BODY:", body.decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
