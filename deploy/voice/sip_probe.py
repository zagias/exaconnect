"""Send one SIP INVITE over UDP and print the first line of the answer.

Usage: python3 sip_probe.py HOST [PORT]. Retries for about 20 seconds while the
server starts. Used by check.sh to show Kamailio refuses unlisted callers.
"""

import socket
import sys
import uuid

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5060
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.connect((host, port))
me, my_port = s.getsockname()
call = uuid.uuid4().hex
msg = (
    f"INVITE sip:15550100@{host} SIP/2.0\r\n"
    f"Via: SIP/2.0/UDP {me}:{my_port};branch=z9hG4bK{call[:12]}\r\n"
    "Max-Forwards: 70\r\n"
    f"From: <sip:probe@{me}>;tag={call[:8]}\r\n"
    f"To: <sip:15550100@{host}>\r\n"
    f"Call-ID: {call}\r\n"
    "CSeq: 1 INVITE\r\n"
    f"Contact: <sip:probe@{me}:{my_port}>\r\n"
    "Content-Length: 0\r\n\r\n"
)
for _ in range(10):
    s.send(msg.encode())
    try:
        print(s.recv(4096).decode(errors="replace").splitlines()[0])
        sys.exit(0)
    except OSError:
        continue
print("no answer")
sys.exit(1)
