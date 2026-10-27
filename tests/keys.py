import base64
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class KeyPair:
    private_key: rsa.RSAPrivateKey
    private_pem_pkcs8: str
    private_pem_pkcs1: str
    public_pem: str


def generate_keypair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KeyPair(
        private_key=key,
        private_pem_pkcs8=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        private_pem_pkcs1=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode(),
        public_pem=key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


def sign_sha256(private_key: rsa.RSAPrivateKey, message: bytes) -> str:
    return base64.b64encode(private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()


def verify_sha256(public_key: rsa.RSAPublicKey, message: bytes, signature: str) -> None:
    public_key.verify(base64.b64decode(signature), message, padding.PKCS1v15(), hashes.SHA256())


def wechat_headers(
    platform_key: rsa.RSAPrivateKey,
    serial: str,
    body: str,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
    lowercase: bool = True,
) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    nonce_value = nonce or secrets.token_hex(8)
    signature = sign_sha256(platform_key, f"{ts}\n{nonce_value}\n{body}\n".encode())
    values = {
        "Wechatpay-Signature": signature,
        "Wechatpay-Timestamp": ts,
        "Wechatpay-Nonce": nonce_value,
        "Wechatpay-Serial": serial,
        "Wechatpay-Signature-Type": "WECHATPAY2-SHA256-RSA2048",
    }
    if lowercase:
        return {key.lower(): value for key, value in values.items()}
    return values


def wechat_resource(
    apiv3_key: str,
    resource: Mapping[str, Any],
    *,
    associated_data: str = "transaction",
) -> dict[str, str]:
    nonce = secrets.token_hex(6)
    ciphertext = AESGCM(apiv3_key.encode()).encrypt(
        nonce.encode(),
        json.dumps(resource, separators=(",", ":"), ensure_ascii=False).encode(),
        associated_data.encode(),
    )
    return {
        "algorithm": "AEAD_AES_256_GCM",
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "nonce": nonce,
        "associated_data": associated_data,
    }


def wechat_notification_body(
    event_type: str,
    resource: Mapping[str, Any],
    notification_id: str,
) -> str:
    return json.dumps(
        {
            "id": notification_id,
            "event_type": event_type,
            "resource_type": "encrypt-resource",
            "resource": dict(resource),
        },
        separators=(",", ":"),
    )


def alipay_sign_content(fields: Mapping[str, str]) -> str:
    unsigned = {key: value for key, value in fields.items() if key not in {"sign", "sign_type"}}
    return "&".join(f"{key}={unsigned[key]}" for key in sorted(unsigned))


def alipay_request_sign_content(fields: Mapping[str, str]) -> str:
    unsigned = {key: value for key, value in fields.items() if key != "sign"}
    return "&".join(f"{key}={unsigned[key]}" for key in sorted(unsigned))


def alipay_notification_body(fields: Mapping[str, str], platform_key: rsa.RSAPrivateKey) -> bytes:
    values = dict(fields)
    values.setdefault("sign_type", "RSA2")
    signature = sign_sha256(platform_key, alipay_sign_content(values).encode())
    values["sign"] = signature
    return urlencode(values).encode()


def alipay_response(
    response_key: str, payload: Mapping[str, Any], platform_key: rsa.RSAPrivateKey
) -> bytes:
    content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signature = sign_sha256(platform_key, content.encode())
    return f'{{"{response_key}":{content},"sign":"{signature}"}}'.encode()


def parse_alipay_form(html: str) -> dict[str, str]:
    action_match = re.search(r'action="([^"]+)"', html)
    if not action_match:
        raise AssertionError("form action not found")
    query = dict(parse_qsl(urlparse(action_match.group(1)).query, keep_blank_values=True))
    hidden = {
        unescape(key): unescape(value)
        for key, value in re.findall(r'name="([^"]+)" value="([^"]*)"', html)
    }
    return {**query, **hidden}
