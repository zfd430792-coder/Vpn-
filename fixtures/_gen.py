import base64
import json
import os

os.makedirs("fixtures", exist_ok=True)

vmess_payload = {
    "v": "2", "ps": "TestVmess", "add": "vm.example.com", "port": "443",
    "id": "a3b4c5d6-e7f8-4901-a2b3-c4d5e6f7a8b9", "aid": "0", "scy": "auto",
    "net": "ws", "type": "none", "host": "vm.example.com", "path": "/vm",
    "tls": "tls", "sni": "vm.example.com",
}
vmess = "vmess://" + base64.b64encode(json.dumps(vmess_payload).encode()).decode()
vless = (
    "vless://11111111-2222-3333-4444-555555555555@vl.example.com:443"
    "?encryption=none&security=tls&sni=vl.example.com&type=ws&path=%2Fws&host=vl.example.com#TestVless"
)
trojan = "trojan://mysecret@tr.example.com:443?security=tls&sni=tr.example.com&type=tcp#TestTrojan"
ss = "ss://" + base64.b64encode(b"aes-256-gcm:mypassword").decode() + "@ss.example.com:8388#TestSS"
hy2 = "hy2://hypass@hy.example.com:443?insecure=0&sni=hy.example.com#TestHy2"

uris = "\n".join([vmess, vless, trojan, ss, hy2])
with open("fixtures/sub-uris.txt", "w") as f:
    f.write(uris)
with open("fixtures/sub-b64.txt", "w") as f:
    f.write(base64.b64encode(uris.encode()).decode())
print("ok")
