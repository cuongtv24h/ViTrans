"""Mã hoá khoá API trong CSDL, dấu vân tay chống trùng, che khoá (SPEC §17.14, §20.5).

Mục đích: lộ CSDL hoặc bản sao lưu KHÔNG kéo theo lộ khoá API. Khoá chủ (POOL_MASTER_KEY) nằm ngoài CSDL và ngoài bản sao lưu.
  * Mã hoá: AES-256-GCM, nonce 12 byte ngẫu nhiên, AAD = id khoá (nên không thể đem bản mã của khoá này gắn sang bản ghi khác),
    khoá con dẫn xuất bằng HKDF-SHA256 từ khoá chủ.
  * Dấu vân tay: HMAC-SHA256 (khoá con riêng) rút gọn 128 bit: phát hiện khoá trùng mà không cần giải mã, và vì dùng khoá chủ nên không dò
    ngược được từ CSDL bị lộ.
Giới hạn trung thực: nếu kẻ tấn công có quyền root trên máy đang chạy thì khoá nằm trong RAM của worker; mã hoá này không cứu được trường hợp đó.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO_ENC = b"visynth/pool/credential-encryption/v1"
INFO_FP = b"visynth/pool/credential-fingerprint/v1"
NONCE_LEN = 12


def new_master_key() -> str:
    """Sinh khoá chủ mới (base64url, 32 byte). Lưu ngoài CSDL và ngoài bản sao lưu (SPEC §20.5)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def load_master_key(value: str) -> bytes:
    """Đọc POOL_MASTER_KEY: 64 ký tự hex hoặc base64url (>= 32 byte sau giải mã)."""
    v = value.strip()
    raw: bytes | None = None
    if len(v) == 64:
        try:
            raw = bytes.fromhex(v)
        except ValueError:
            raw = None
    if raw is None:
        try:
            raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        except (binascii.Error, ValueError) as e:
            raise ValueError("POOL_MASTER_KEY không phải hex hay base64url hợp lệ") from e
    if len(raw) < 32:
        raise ValueError("POOL_MASTER_KEY phải có ít nhất 32 byte")
    return raw


def _derive(master: bytes, info: bytes) -> bytes:
    if len(master) < 32:
        raise ValueError("khoá chủ phải có ít nhất 32 byte")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


def encrypt_secret(master: bytes, credential_id: str, secret: str, nonce: bytes | None = None) -> bytes:
    """Trả `nonce || bản mã || thẻ xác thực`. `nonce` chỉ truyền vào để test; production luôn để ngẫu nhiên."""
    nonce = nonce or os.urandom(NONCE_LEN)
    return nonce + AESGCM(_derive(master, INFO_ENC)).encrypt(nonce, secret.encode("utf-8"), credential_id.encode("utf-8"))


def decrypt_secret(master: bytes, credential_id: str, blob: bytes) -> str:
    """Ném cryptography.exceptions.InvalidTag nếu sai khoá chủ, sai id khoá (AAD) hoặc bản mã bị sửa."""
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return AESGCM(_derive(master, INFO_ENC)).decrypt(nonce, ct, credential_id.encode("utf-8")).decode("utf-8")


def fingerprint(master: bytes, secret: str) -> str:
    return hmac.new(_derive(master, INFO_FP), secret.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def last4(secret: str) -> str:
    """4 ký tự cuối. Khoá ngắn hơn 8 ký tự không lộ gì cả."""
    return secret[-4:] if len(secret) >= 8 else "****"


def mask(secret: str) -> str:
    return "\u2026" + last4(secret)
