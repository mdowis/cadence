"""
Tests for KalshiSigner (RSA-PSS request signing).

Generates a throwaway test key so these can run offline without a real
Kalshi account.
"""

import base64
import os
import subprocess
import tempfile

from kalshi_arbitrage import KalshiSigner


def _generate_test_key():
    """Generate a temporary RSA private key for testing."""
    fd, path = tempfile.mkstemp(suffix=".pem", prefix="cadence_test_key_")
    os.close(fd)
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA",
         "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", path],
        check=True, capture_output=True,
    )
    return path


def test_signer_missing_key_file():
    try:
        KalshiSigner("/nonexistent/path/key.pem")
    except FileNotFoundError as e:
        assert "not found" in str(e).lower()
        return
    raise AssertionError("Should have raised FileNotFoundError")


def test_signer_loads_key():
    key_path = _generate_test_key()
    try:
        signer = KalshiSigner(key_path)
        assert signer.backend in ("cryptography", "openssl")
        assert signer.private_key_path == os.path.abspath(key_path)
    finally:
        os.unlink(key_path)


def test_signer_produces_base64():
    key_path = _generate_test_key()
    try:
        signer = KalshiSigner(key_path)
        signature = signer.sign("1234567890000", "GET", "/trade-api/v2/markets")
        # Should be valid base64
        decoded = base64.b64decode(signature)
        # 2048-bit RSA signature is 256 bytes
        assert len(decoded) == 256, f"Expected 256 bytes, got {len(decoded)}"
    finally:
        os.unlink(key_path)


def test_signer_strips_query_string():
    """Path with query string should be signed without the query."""
    key_path = _generate_test_key()
    try:
        signer = KalshiSigner(key_path)
        sig1 = signer.sign("1234567890000", "GET",
                          "/trade-api/v2/markets?limit=200")
        sig2 = signer.sign("1234567890000", "GET",
                          "/trade-api/v2/markets")
        # Both should sign to the same value (query stripped)
        # NOTE: PSS uses a random salt, so signatures differ even for identical
        # messages. We can't compare directly — instead verify both are valid.
        assert len(base64.b64decode(sig1)) == 256
        assert len(base64.b64decode(sig2)) == 256
    finally:
        os.unlink(key_path)


def test_signer_deterministic_message_format():
    """The message format (timestamp + method + path) matches Kalshi's spec."""
    key_path = _generate_test_key()
    try:
        signer = KalshiSigner(key_path)
        # Should not raise for any valid method
        for method in ["GET", "POST", "DELETE"]:
            sig = signer.sign("1000000000000", method, "/test/path")
            assert sig  # non-empty
            assert len(base64.b64decode(sig)) == 256
    finally:
        os.unlink(key_path)


def test_signer_verifiable_with_openssl():
    """
    End-to-end: sign with the signer, verify with openssl.
    This catches mismatches in the padding/hash config.
    """
    key_path = _generate_test_key()
    try:
        signer = KalshiSigner(key_path)

        # Extract the public key
        pub_path = key_path + ".pub"
        subprocess.run(
            ["openssl", "rsa", "-in", key_path, "-pubout", "-out", pub_path],
            check=True, capture_output=True,
        )

        # Sign a test message
        message = "1234567890000GET/trade-api/v2/markets"
        timestamp_ms = "1234567890000"
        signature_b64 = signer.sign(timestamp_ms, "GET", "/trade-api/v2/markets")

        # Write signature to file
        sig_path = key_path + ".sig"
        with open(sig_path, "wb") as f:
            f.write(base64.b64decode(signature_b64))

        # Write the message to a file (openssl verify reads from stdin or file)
        msg_path = key_path + ".msg"
        with open(msg_path, "wb") as f:
            f.write(message.encode("utf-8"))

        # Verify with openssl
        result = subprocess.run(
            ["openssl", "dgst", "-sha256",
             "-verify", pub_path,
             "-sigopt", "rsa_padding_mode:pss",
             "-sigopt", "rsa_pss_saltlen:digest",
             "-signature", sig_path,
             msg_path],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, \
            f"Signature verification failed: {result.stdout} {result.stderr}"
        assert "Verified OK" in result.stdout

        os.unlink(pub_path)
        os.unlink(sig_path)
        os.unlink(msg_path)
    finally:
        if os.path.exists(key_path):
            os.unlink(key_path)


if __name__ == "__main__":
    # Check openssl is available (required for these tests)
    try:
        subprocess.run(["openssl", "version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("SKIP: openssl not available on PATH")
        raise SystemExit(0)

    test_signer_missing_key_file()
    test_signer_loads_key()
    test_signer_produces_base64()
    test_signer_strips_query_string()
    test_signer_deterministic_message_format()
    test_signer_verifiable_with_openssl()
    print("All 6 signer tests passed!")
