"""HLS AES-128 解密。

HLS 的 AES-128 分片采用 CBC + PKCS7 填充。cryptography 的
Cipher 上下文不做自动补位 / 去位，因此解密后需要手动剥离填充；
遇到不合法的填充时保留原样（部分非标准编码器会省略填充）。
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class DecryptError(ValueError):
    """密钥 / IV / 密文不合法。"""


def sequence_iv(sequence: int) -> bytes:
    """EXT-X-KEY 未给 IV 时，用分片序号的大端 16 字节作为 IV。"""
    return int(sequence).to_bytes(16, byteorder="big", signed=False)


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """解密单个分片；失败时抛出 DecryptError。"""
    if len(key) != 16:
        raise DecryptError(f"AES-128 密钥长度错误：{len(key)} 字节")
    if len(iv) != 16:
        raise DecryptError(f"AES-128 IV 长度错误：{len(iv)} 字节")
    if not data:
        return b""
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
    except ValueError as exc:
        raise DecryptError(f"AES 解密失败：{exc}") from exc
    return _strip_pkcs7(plain)


def _strip_pkcs7(data: bytes) -> bytes:
    """剥离 PKCS7 填充；填充字节不合法时按无填充处理。"""
    if not data:
        return data
    padding_length = data[-1]
    if 1 <= padding_length <= 16 and data.endswith(bytes([padding_length]) * padding_length):
        return data[:-padding_length]
    return data
