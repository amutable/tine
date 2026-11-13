# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Release verifier against the real `sqv` binary.

The tests create an archive with gpg, which makes the keys and signs the Release. sqv verifies
the Release, as it does in the driver. All keys and signatures have fixed dates, so each test
controls what has expired at the time of the verification.
"""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple, override

import release

import snapshotter
import verify

# The keys are made on day one and the Release is signed on day two. The snapshot is pinned to
# day three.
MADE = "20260101T000000"
SIGNED = "20260102T000000"
BEFORE_SIGNING = "2026-01-01T12:00:00Z"
PINNED = "2026-01-03T00:00:00Z"
LATER_SIGNING = "20260115T000000"
LATER = "2026-02-01T00:00:00Z"
# The `Date` and `Valid-Until` fields of the Release. It is published on the day of the signature
# and valid for one week.
DATED = "Fri, 02 Jan 2026 00:00:00 UTC"
VALID_UNTIL = "Fri, 09 Jan 2026 00:00:00 UTC"
# The directory of the suite in the repository, and the directory of the index in the suite.
SUITE = "dists/testing"
INDEX = "main/binary-amd64"


class Declared(NamedTuple):
    """The keys and the time that a repository declares for the verification of its Release."""

    keys: dict[str, str]
    time: str | None
    scratch: Path


class Archive:
    """A gpg home directory with the keys of an archive."""

    def __init__(self, home: Path) -> None:
        self.home = home
        home.mkdir(mode=0o700)
        self.key = self._generate("archive")
        self.previous = self._generate("previous")
        self.stray = self._generate("stray")
        self.stale = self._generate("stale", expire="7d")

    def gpg(self, *args: str, at: str = MADE, stdin: str | None = None) -> str:
        command = [
            "gpg",
            "--homedir",
            str(self.home),
            "--batch",
            "--no-tty",
            "--faked-system-time",
            at,
            "--quiet",
        ]
        return subprocess.run(
            [*command, *args], input=stdin, capture_output=True, check=True, text=True
        ).stdout

    def _generate(self, name: str, expire: str = "never") -> str:
        self.gpg(
            "--passphrase", "", "--pinentry-mode", "loopback",
            "--quick-generate-key", f"{name} <{name}@tine.test>", "ed25519", "sign", expire,
        )  # fmt: skip
        listing = self.gpg("--with-colons", "--list-keys", f"{name}@tine.test")
        return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")).upper()

    def key_file(self, fingerprint: str, directory: Path) -> Path:
        path = directory / f"{fingerprint}.key"
        path.write_text(self.gpg("--export", "--armor", fingerprint))
        return path

    def clearsign(self, text: str, *signers: str, at: str = SIGNED) -> bytes:
        """Return the text with a cleartext signature of every signer."""
        users = [argument for signer in signers for argument in ("--local-user", signer)]
        return self.gpg(*users, "--clearsign", stdin=text, at=at).encode()


class TestVerify(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        scratch = Path(cls._scratch.name)
        cls.archive = Archive(scratch / "archive")
        cls.addClassCleanup(
            subprocess.run, ["gpgconf", "--homedir", str(cls.archive.home), "--kill", "all"], check=True
        )
        cls.pool = scratch / "pool"
        cls.pool.mkdir()

    def package(self, name: str, content: bytes | None = None) -> tuple[Path, str]:
        """Write a package into the pool under its checksum. Return its path and its index record."""
        content = content if content is not None else f"{name} contents".encode()
        checksum = hashlib.sha256(content).hexdigest()
        path = self.pool / f"{checksum}.deb"
        path.write_bytes(content)
        record = (
            f"Package: {name}\nVersion: 1\nArchitecture: amd64\n"
            f"Filename: pool/main/{name}_1_amd64.deb\nSize: {len(content)}\nSHA256: {checksum}\n\n"
        )
        return path, record

    def repository(
        self,
        *records: str,
        signers: tuple[str, ...] | None = None,
        date: str = DATED,
        valid_until: str | None = None,
        stated_at: str = INDEX,
        signed_at: str = SIGNED,
        suite: str = "testing",
    ) -> Path:
        """Write a pinned repository: an index of the records and a signed Release that lists it."""
        directory = Path(tempfile.mkdtemp(dir=self._scratch.name)) / "repo"
        (directory / SUITE / INDEX).mkdir(parents=True)
        index = "".join(records).encode()
        (directory / SUITE / INDEX / "Packages").write_bytes(index)
        expires = f"Valid-Until: {valid_until}\n" if valid_until is not None else ""
        text = (
            f"Origin: tine\nSuite: {suite}\nDate: {date}\n{expires}"
            "Architectures: amd64\nComponents: main\nSHA256:\n"
            f" {hashlib.sha256(index).hexdigest()} {len(index)} {stated_at}/Packages\n"
        )
        signers = signers if signers is not None else (self.archive.key,)
        signed = self.archive.clearsign(text, *signers, at=signed_at) if signers else text.encode()
        (directory / SUITE / release.INRELEASE).write_bytes(signed)
        return directory

    def flat_repository(
        self,
        *records: str,
        signers: tuple[str, ...] | None = None,
        date: str = DATED,
        valid_until: str | None = None,
        signed_at: str = SIGNED,
    ) -> Path:
        """Write a pinned repository like repository(), but with a flat layout."""
        directory = Path(tempfile.mkdtemp(dir=self._scratch.name)) / "repo"
        directory.mkdir()
        index = "".join(records).encode()
        (directory / "Packages").write_bytes(index)
        expires = f"Valid-Until: {valid_until}\n" if valid_until is not None else ""
        text = (
            f"Origin: tine\nComponent: deb\nDate: {date}\n{expires}"
            "Architectures: amd64\nSHA256:\n"
            f" {hashlib.sha256(index).hexdigest()} {len(index)} Packages\n"
        )
        signers = signers if signers is not None else (self.archive.key,)
        signed = self.archive.clearsign(text, *signers, at=signed_at) if signers else text.encode()
        (directory / release.INRELEASE).write_bytes(signed)
        return directory

    def retain(self, repository: Path, older: Path, pinned_at: str | None = None) -> None:
        """Move the repository `older` into `repository` as a retained generation."""
        retained = repository / snapshotter.RETAINED
        retained.mkdir(exist_ok=True)
        generation = retained / str(len(list(retained.iterdir())))
        older.rename(generation)
        if pinned_at is not None:
            (generation / snapshotter.PINNED_AT).write_text(pinned_at + "\n")

    def declared(
        self, *declared: str, files: dict[str, Path] | None = None, time: str | None = PINNED
    ) -> Declared:
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        if files is None:
            files = {f: self.archive.key_file(f, scratch) for f in declared}
        return Declared({f: str(path) for f, path in files.items()}, time, scratch)

    def verify(self, declared: Declared, repository: Path, *packages: Path) -> Path:
        out = declared.scratch / "verified"
        verify.verify(
            verify.Spec(
                keys=declared.keys,
                out=str(out),
                packages={f"pkg--{p.name}": str(p) for p in packages},
                repository=str(repository),
                time=declared.time,
            )
        )
        return out

    def test_rejects_a_key_file_declared_under_another_fingerprint(self) -> None:
        # The key file holds the key that signed the Release, but the repository declares it
        # under the fingerprint of another key. No signature is from the declared fingerprint.
        package, record = self.package("undeclared")
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        declared = self.declared(
            files={self.archive.previous: self.archive.key_file(self.archive.key, scratch)}
        )
        with self.assertRaises(SystemExit) as failure:
            self.verify(declared, self.repository(record), package)
        self.assertIn("declared under another fingerprint", str(failure.exception))

    def test_rejects_a_key_file_that_is_no_key(self) -> None:
        package, record = self.package("junk")
        scratch = Path(tempfile.mkdtemp(dir=self._scratch.name))
        (scratch / "junk.key").write_text("<html>Access Denied</html>\n")
        declared = self.declared(files={self.archive.key: scratch / "junk.key"})
        with self.assertRaises(SystemExit) as failure:
            self.verify(declared, self.repository(record), package)
        self.assertIn("no valid signature from the declared keys", str(failure.exception))

    def test_rejects_the_release_of_another_suite(self) -> None:
        # The signature is valid, because an archive signs every suite with the same keys. Only
        # the suite name is wrong.
        package, record = self.package("oldstable")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), self.repository(record, suite="oldstable"), package)
        self.assertIn("not 'testing'", str(failure.exception))

    def test_refuses_a_time_it_cannot_read(self) -> None:
        package, record = self.package("untimed")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key, time="yesterday"), self.repository(record), package)
        self.assertIn("not an ISO 8601 time", str(failure.exception))

    def test_publishes_a_package_the_signed_release_vouches_for(self) -> None:
        package, record = self.package("good")
        for repository in [self.repository(record), self.flat_repository(record)]:
            with self.subTest(repository=repository):
                out = self.verify(self.declared(self.archive.key), repository, package)
                self.assertEqual((out / f"pkg--{package.name}").read_bytes(), package.read_bytes())

    def test_one_declared_key_among_the_signers_suffices(self) -> None:
        # The archive signs with its current key and with the previous key, so a repository can
        # declare either one. An additional signature from an undeclared key does not matter.
        package, record = self.package("either")
        signers = (self.archive.stray, self.archive.key, self.archive.previous)
        # Each declared key is in its own file, in the armored format that the archive serves.
        for declared in (
            (self.archive.key,),
            (self.archive.previous,),
            (self.archive.key, self.archive.previous),
        ):
            with self.subTest(declared=declared):
                out = self.verify(
                    self.declared(*declared), self.repository(record, signers=signers), package
                )
                self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_rejects_a_release_no_declared_key_signed(self) -> None:
        package, record = self.package("stray")
        for signers in ((self.archive.stray,), ()):
            with self.subTest(signers=signers), self.assertRaises(SystemExit) as failure:
                self.verify(
                    self.declared(self.archive.key), self.repository(record, signers=signers), package
                )
            self.assertIn("no valid signature from the declared keys", str(failure.exception))

    def test_rejects_a_tampered_release(self) -> None:
        package, record = self.package("tampered-release")
        repository = self.repository(record)
        signed = repository / SUITE / release.INRELEASE
        signed.write_bytes(signed.read_bytes().replace(b"Suite: testing", b"Suite: unstable"))
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), repository, package)
        self.assertIn("no valid signature from the declared keys", str(failure.exception))

    def test_rejects_an_index_the_release_does_not_state(self) -> None:
        package, record = self.package("unstated")
        _, extra = self.package("extra")
        repository = self.repository(record)
        (repository / SUITE / INDEX / "Packages").write_text(record + extra)
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), repository, package)
        self.assertIn("states no main/binary-amd64/Packages", str(failure.exception))

    def test_rejects_an_index_the_release_states_at_another_path(self) -> None:
        # The Release lists the checksum of the index, but at the path of another component.
        package, record = self.package("elsewhere")
        repository = self.repository(record, stated_at="contrib/binary-amd64")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), repository, package)
        self.assertIn("states no main/binary-amd64/Packages", str(failure.exception))

    def test_rejects_a_release_that_had_expired_by_the_pinned_snapshot(self) -> None:
        package, record = self.package("stale")
        repository = self.repository(record, valid_until="Fri, 02 Jan 2026 12:00:00 UTC")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), repository, package)
        self.assertIn("expired at", str(failure.exception))

    def test_a_release_still_current_at_the_pin_verifies(self) -> None:
        package, record = self.package("current")
        repository = self.repository(record, valid_until=VALID_UNTIL)
        out = self.verify(self.declared(self.archive.key), repository, package)
        self.assertEqual([path.name for path in out.iterdir()], [f"pkg--{package.name}"])

    def test_rejects_a_package_the_index_does_not_describe(self) -> None:
        package, _ = self.package("undescribed")
        _, record = self.package("described")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), self.repository(record), package)
        self.assertIn("vouches for checksum", str(failure.exception))

    def test_rejects_a_tampered_package(self) -> None:
        package, record = self.package("tampered")
        package.write_bytes(b"something else")
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), self.repository(record), package)
        self.assertIn("not the checksum it is named by", str(failure.exception))

    def test_rejects_a_pool_file_not_named_by_checksum(self) -> None:
        package, record = self.package("misnamed")
        misnamed = package.with_name("misnamed_1_amd64.deb")
        misnamed.write_bytes(package.read_bytes())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), self.repository(record), misnamed)
        self.assertIn("not named by its checksum", str(failure.exception))

    def test_refuses_a_signature_from_after_the_pinned_snapshot(self) -> None:
        # The verifier refuses a signature that was made after the pinned time. The result
        # therefore does not depend on the time of the build.
        package, record = self.package("early")
        repository = self.repository(record)
        out = self.verify(self.declared(self.archive.key), repository, package)
        self.assertTrue((out / f"pkg--{package.name}").exists())
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key, time=BEFORE_SIGNING), repository, package)
        self.assertIn("no valid signature", str(failure.exception))

    def test_a_key_valid_when_it_signed_stays_so(self) -> None:
        # sqv checks a key at the time of the signature. The key expires a week after it was
        # made, and its signature stays valid at every later time.
        package, record = self.package("stale")
        repository = self.repository(record, signers=(self.archive.stale,))
        for time in (PINNED, LATER, None):
            with self.subTest(time=time):
                out = self.verify(self.declared(self.archive.stale, time=time), repository, package)
                self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_publishes_a_package_only_a_retained_release_vouches_for(self) -> None:
        # The pinned index no longer describes the package `dropped`. A lock that selected the
        # package retains the older Release and index, which still describe it.
        dropped, dropped_record = self.package("dropped")
        current, current_record = self.package("current")
        repository = self.repository(current_record)
        self.retain(repository, self.repository(dropped_record))
        out = self.verify(self.declared(self.archive.key), repository, dropped, current)
        for package in (dropped, current):
            self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_judges_a_retained_release_as_of_its_own_pin(self) -> None:
        # After a pin is rolled back, a lock can retain a Release that was signed after the pinned
        # time of the repository. The verifier must check it at the time that the lock recorded.
        package, record = self.package("later")
        repository = self.repository()
        self.retain(repository, self.repository(record, signed_at=LATER_SIGNING))
        declared = self.declared(self.archive.key)
        with self.assertRaises(SystemExit) as failure:
            self.verify(declared, repository, package)
        self.assertIn("no valid signature", str(failure.exception))
        (repository / snapshotter.RETAINED / "0" / snapshotter.PINNED_AT).write_text(LATER + "\n")
        out = self.verify(self.declared(self.archive.key), repository, package)
        self.assertTrue((out / f"pkg--{package.name}").exists())

    def test_rejects_a_retained_release_no_declared_key_signed(self) -> None:
        package, record = self.package("retained-stray")
        repository = self.repository()
        self.retain(repository, self.repository(record, signers=(self.archive.stray,)))
        with self.assertRaises(SystemExit) as failure:
            self.verify(self.declared(self.archive.key), repository, package)
        self.assertIn("no valid signature", str(failure.exception))

    def test_authenticates_a_retained_release_only_for_what_the_pinned_one_lacks(self) -> None:
        # The verifier reads a retained generation only for a package that the pinned index lacks.
        # A retained Release with a bad signature fails these packages and no others.
        current, current_record = self.package("current-only")
        dropped, dropped_record = self.package("dropped-unsigned")
        repository = self.repository(current_record)
        self.retain(repository, self.repository(dropped_record, signers=(self.archive.stray,)))
        out = self.verify(self.declared(self.archive.key), repository, current)
        self.assertTrue((out / f"pkg--{current.name}").exists())
        with self.assertRaises(SystemExit):
            self.verify(self.declared(self.archive.key), repository, current, dropped)

    def test_a_batch_names_the_rejected_package_only(self) -> None:
        good, good_record = self.package("batch-good")
        bad, _ = self.package("batch-bad")
        declared = self.declared(self.archive.key)
        with self.assertRaises(SystemExit) as failure:
            self.verify(declared, self.repository(good_record), good, bad)
        self.assertIn(bad.name, str(failure.exception))
        self.assertNotIn(good.name, str(failure.exception))
        self.assertFalse((declared.scratch / "verified" / f"pkg--{good.name}").exists())


if __name__ == "__main__":
    unittest.main()
