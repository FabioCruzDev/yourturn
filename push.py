"""Web Push (RFC 8030/8291/8292) só com a biblioteca `cryptography` do sistema: avisa no celular quando uma sessão
passa a aguardar você, mesmo com o app fechado. O navegador entrega pelo serviço de push dele (FCM no Chrome).

- VAPID: chave EC P-256 em ~/.config/kanban-claude/vapid.pem (criada na 1ª vez); a pública vai para o navegador.
- Conteúdo cifrado em aes128gcm (RFC 8291): só o navegador que assinou consegue ler.
"""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VAPID_FILE = Path("~/.config/kanban-claude/vapid.pem").expanduser()
# Quem assina as notificações (claim "sub" do VAPID): a URL do quadro, sem e-mail nenhum
_HOSTS = [h.strip() for h in os.environ.get("KANBAN_HOSTS", "").split(",") if h.strip()]
VAPID_SUB = os.environ.get("KANBAN_PUSH_SUB") or (f"https://{_HOSTS[0]}" if _HOSTS else "https://localhost")
# Só serviços de push conhecidos: o servidor não faz POST para qualquer endereço que alguém registrar
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com", ".notify.windows.com")

b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
unb64u = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_key():
    if not VAPID_FILE.exists():
        VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        VAPID_FILE.write_bytes(pem)
        VAPID_FILE.chmod(0o600)
    return serialization.load_pem_private_key(VAPID_FILE.read_bytes(), None)


def public_key():
    """Chave pública VAPID em base64url, para o pushManager.subscribe do navegador."""
    return b64u(raw_public(vapid_key()))


def vapid_auth(endpoint, key):
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUB}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(raw_public(key))}"


def encrypt(p256dh, auth, payload, salt=None, server_key=None):
    """aes128gcm da RFC 8291 (um registro só). salt/server_key só se passam em teste."""
    ua_pub = unb64u(p256dh)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = raw_public(as_key)
    secret = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, unb64u(auth), b"WebPush: info\x00" + ua_pub + as_pub).derive(secret)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02 = último registro
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


def allowed(endpoint):
    u = urlparse(endpoint or "")
    return u.scheme == "https" and any(u.hostname == h or (h.startswith(".") and (u.hostname or "").endswith(h)) for h in PUSH_HOSTS)


def send(sub, message, ttl=3600):
    """Manda uma notificação. Devolve o status HTTP; 404/410 = inscrição expirada (o chamador apaga)."""
    endpoint = sub["endpoint"]
    if not allowed(endpoint):
        raise ValueError("serviço de push desconhecido")
    data = encrypt(sub["keys"]["p256dh"], sub["keys"]["auth"], json.dumps(message, ensure_ascii=False).encode())
    req = urllib.request.Request(endpoint, data=data, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "high",
        "Authorization": vapid_auth(endpoint, vapid_key())})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
