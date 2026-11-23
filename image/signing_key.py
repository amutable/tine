#!/usr/bin/python3
# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Derive a development signing key pair from its certificate's common name.

Everything in the pair is a function of the common name, so every build, workspace and project that
uses the same name gets the same pair. Anyone who knows the name can compute the private key.

Projects that pin different tine revisions must derive the same pair from the same name, or their
images stop trusting each other's signatures. Never change how the key, the issuer or the serial number
are derived. signing_key_test.py pins them.
"""

import datetime
import hashlib
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

# RFC 5280 reserves 99991231235959Z for a certificate without an expiration date.
_NOT_VALID_BEFORE = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
_NOT_VALID_AFTER = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)


def _derive_bytes(common_name: str, purpose: str, size: int) -> bytes:
    return hashlib.shake_256(b"\0".join([common_name.encode(), purpose.encode()])).digest(size)


def derive(common_name: str) -> tuple[bytes, bytes]:
    """Return the PEM private key and PEM certificate for the common name."""
    # The kernel can't verify a certificate with an Ed25519 key, so dm-verity signatures need ECDSA or
    # RSA. A P-256 private key must be below the order of the curve, and every 31-byte value is.
    key = ec.derive_private_key(int.from_bytes(_derive_bytes(common_name, "key", 31)), ec.SECP256R1())

    # systemd-repart identifies the signer of a verity signature by the issuer and serial number of its
    # certificate. The kernel and the userspace check against /usr/lib/verity.d look up the certificate by
    # both, so both must be derived from the name for a certificate from another build to match.
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, common_name)])
    # RFC 5280 limits the serial number to 20 bytes. DER adds a zero byte to a value whose top bit is set.
    serial = int.from_bytes(_derive_bytes(common_name, "serial", 19))
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .not_valid_before(_NOT_VALID_BEFORE)
        .not_valid_after(_NOT_VALID_AFTER)
        .serial_number(serial)
        .public_key(key.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # An ECDSA signature contains a random nonce by default. RFC 6979 derives the nonce from the key
        # and the message instead, so the certificate is the same on every run.
        .sign(private_key=key, algorithm=hashes.SHA256(), ecdsa_deterministic=True)
    )

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key_pem, certificate.public_bytes(serialization.Encoding.PEM)


def main() -> None:
    common_name, key_path, certificate_path = sys.argv[1:]
    key_pem, certificate_pem = derive(common_name)
    Path(key_path).write_bytes(key_pem)
    Path(certificate_path).write_bytes(certificate_pem)


if __name__ == "__main__":
    main()
