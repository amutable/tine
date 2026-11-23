# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for deriving a development signing key pair from its common name.

buck test tine//image:test
"""

import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import signing_key


class DeriveTest(unittest.TestCase):
    def test_same_name_same_pair(self) -> None:
        # Builds in different workspaces must derive the same pair. Derive the pair twice from the same
        # name and check that the key and the certificate are byte-identical.
        self.assertEqual(
            signing_key.derive("tine.generated.verity"), signing_key.derive("tine.generated.verity")
        )

    def test_known_answer(self) -> None:
        # Projects that pin different tine revisions must derive the same pair from the same name.
        # Derive the pair for a fixed name and compare the public key, the issuer and the serial number
        # with the values that earlier revisions derived. A signature matches a certificate by those
        # three, so the rest of the certificate may change.
        _, certificate_pem = signing_key.derive("tine.generated.verity")
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        self.assertEqual(
            certificate.public_key()
            .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            .hex(),
            "3059301306072a8648ce3d020106082a8648ce3d030107034200046de4e9d1f314dd10fdbe68ad79ba9c42a10b15dc"
            "74082ccf2b45885a4af2e6988072bc9b100468e7829fbd016463b07ce95110c69e2c14b008efe2163c29768c",
        )
        self.assertEqual(certificate.issuer.rfc4514_string(), "CN=tine.generated.verity")
        self.assertEqual(certificate.serial_number, 0x5F2C5133861D5C56A4B6A91C03692FBBA03AFB)

    def test_other_name_other_pair(self) -> None:
        # Two names must not share a key. Derive pairs for two names and check that the keys and the
        # serial numbers differ.
        key_a, certificate_a = signing_key.derive("tine.generated.verity")
        key_b, certificate_b = signing_key.derive("tine.generated.secureboot")
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(
            x509.load_pem_x509_certificate(certificate_a).serial_number,
            x509.load_pem_x509_certificate(certificate_b).serial_number,
        )

    def test_certificate_matches_key(self) -> None:
        # The kernel rejects a self-signed certificate whose signature doesn't verify. Derive a pair and
        # check that the key is P-256, that the certificate has the common name, and that the key signed
        # the certificate. The signature only verifies if the certificate contains the key's public key.
        key_pem, certificate_pem = signing_key.derive("tine.generated.verity")
        key = serialization.load_pem_private_key(key_pem, password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        self.assertIsInstance(key.curve, ec.SECP256R1)

        certificate = x509.load_pem_x509_certificate(certificate_pem)
        self.assertEqual(certificate.subject.rfc4514_string(), "CN=tine.generated.verity")
        certificate.verify_directly_issued_by(certificate)


if __name__ == "__main__":
    unittest.main()
