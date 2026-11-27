"""AES-128 解密的单元测试。"""

from __future__ import annotations

import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from muxer.protocols.hls.crypto import DecryptError, decrypt_segment, sequence_iv

KEY = bytes(range(16))
IV = bytes(range(16, 32))


def encrypt(data: bytes, key: bytes = KEY, iv: bytes = IV) -> bytes:
    """按 HLS 约定构造 PKCS7 填充的密文。"""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class CryptoTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        payload = b"muxer hls segment payload" * 100
        ciphertext = encrypt(payload)
        self.assertNotEqual(ciphertext[:32], payload[:32])
        self.assertEqual(decrypt_segment(ciphertext, KEY, IV), payload)

    def test_sequence_iv(self) -> None:
        self.assertEqual(sequence_iv(0), b"\x00" * 16)
        self.assertEqual(sequence_iv(1), b"\x00" * 15 + b"\x01")
        self.assertEqual(len(sequence_iv(123456)), 16)

    def test_invalid_key(self) -> None:
        with self.assertRaises(DecryptError):
            decrypt_segment(b"data", b"short", IV)

    def test_invalid_ciphertext(self) -> None:
        with self.assertRaises(DecryptError):
            decrypt_segment(b"not-a-block-multiple", KEY, IV)

    def test_empty_payload(self) -> None:
        self.assertEqual(decrypt_segment(b"", KEY, IV), b"")


if __name__ == "__main__":
    unittest.main()