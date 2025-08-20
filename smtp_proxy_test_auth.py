#!/usr/bin/env python3
import sys
import socks
import socket
import time

if len(sys.argv) not in (5,7):
    print("Usage: python smtp_proxy_test_auth.py <mx_host> <mx_port> <proxy_host> <proxy_port> [proxy_user proxy_pass]")
    sys.exit(2)

mx_host = sys.argv[1]
mx_port = int(sys.argv[2])
proxy_host = sys.argv[3]
proxy_port = int(sys.argv[4])

proxy_user = None
proxy_pass = None
if len(sys.argv) == 7:
    proxy_user = sys.argv[5]
    proxy_pass = sys.argv[6]

print(f"[TEST] Connecting to MX {mx_host}:{mx_port} via SOCKS5 {proxy_host}:{proxy_port} (auth={'yes' if proxy_user else 'no'}) ...")
socks.set_default_proxy(socks.SOCKS5, proxy_host, proxy_port, rdns=True, username=proxy_user, password=proxy_pass)
s = socks.socksocket()
s.settimeout(20)
try:
    s.connect((mx_host, mx_port))
    print("[TEST] Connected, receiving banner...")
    banner = s.recv(1024)
    print("Banner:", banner.decode(errors='replace').strip())
    # send minimal EHLO and read reply
    s.sendall(b"EHLO test.example.com\r\n")
    time.sleep(0.6)
    data = b""
    try:
        while True:
            part = s.recv(4096)
            if not part:
                break
            data += part
            if len(part) < 4096:
                break
    except socket.timeout:
        pass
    if data:
        print("EHLO response:", data.decode(errors='replace').strip())
    else:
        print("[TEST] No EHLO response received (timeout or immediate close).")
    try:
        s.sendall(b"QUIT\r\n")
    except:
        pass
except Exception as e:
    print("Error:", repr(e))
finally:
    try:
        s.close()
    except:
        pass