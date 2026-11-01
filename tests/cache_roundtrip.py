# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Prove the shared cache end to end: a real Buck publishes through the shim, and a cold one gets it back.

    buck run tine//tests:cache-roundtrip

Everything the shim does on its own is unit-tested in remote_cache/. What only a real build shows is
that `tine buck` starts and shares the shim, that Buck's client and the shim agree on the protocol,
and that a result one build published is a hit for a machine holding nothing but the bucket. So this
stands up a SeaweedFS bucket, points a `tine.local.toml` at it, builds the examples cold as a
builder, then cleans, stops the shim, drops its store, and builds again as a reader.

One example per supported component type (see SERVED), each checked through `what-ran`.

Runs in a dedicated isolation dir, to avoid messing up the developer's own build state.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import cache_shim
from util import buck2_argv, buck_output, fail

import seaweed

# What a warm build has to take from the cache, and through which actions.
SERVED = {
    "tine//examples/image-go-project:hello": ("go_fetch", "go_build"),
    "tine//examples/image-go-project:nodeps": ("go_build",),
    "tine//examples/image-rust-project:hello": ("cargo_build",),
    "tine//examples/package/rpm:hello-tree": ("rpmbuild",),
}
# `<target> (<configuration>) (<category>)`, as `what-ran` names an action.
ACTION = re.compile(r"(?P<target>\S+) \(\S+\) \((?P<category>\w+)\)")
ISOLATION = "cache-roundtrip"
# The platform whose executor decides whether Buck offers the shim an upload at all.
PLATFORM = "tine//platform:default"
BUCKET = "tine-cache"
# `tine.LOCAL_SETTINGS`, which this cannot import without dragging the whole launcher in.
LOCAL_SETTINGS = "tine.local.toml"
# Counters a healthy round trip never touches. A reader's executor offers no uploads, so not even
# Buck's write probe arrives; a refusal means it offered one anyway, causing a noisy upload warning
# for every cacheable action.
QUIET = (
    "bucket errors",
    "bundles gone",
    "bundles refused",
    "bundles too large",
    "incomplete",
    "pointers refused",
    "publish failures",
    "uploads refused",
)


def toml(table: Mapping[str, object]) -> str:
    """The `[cache]` table as TOML."""
    lines = ["[cache]"]
    for key, value in table.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, str):
            lines.append(f"{key} = {value!r}".replace("'", '"'))
        else:
            assert isinstance(value, list)
            lines.append(f"{key} = [{', '.join(repr(one).replace(chr(39), chr(34)) for one in value)}]")
    return "\n".join(lines) + "\n"


class RoundTrip:
    """One round trip's moving parts: the checkout, the bucket, the settings and the shim."""

    def __init__(self, root: Path, scratch: Path, weed: seaweed.Seaweed) -> None:
        self.root = root
        self.reader: dict[str, object] = {
            "read_url": weed.read_url,
            "authority": [str(scratch / "ca.pem")],
            "dir": str(scratch / "store"),
        }
        self.builder = self.reader | {
            "s3_bucket": BUCKET,
            "s3_endpoint": weed.s3_endpoint,
            "s3_key_file": str(scratch / "s3.key"),
            "s3_insecure": True,
            "signing_key": str(scratch / "leaf.key"),
            "signing_certificate": str(scratch / "leaf.pem"),
        }
        (scratch / "s3.key").write_text("unchecked unchecked\n")
        self.settle(self.builder)
        # A nested command has to behave as a developer's would: start the shim rather than assume
        # the caller did, and run the launcher rather than the Buck the caller exported.
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if name not in ("BUCK2_BINARY", "BUCK2_ARG0", "TINE_MOUNTS")
        }

    def settle(self, table: dict[str, object]) -> None:
        """Write the `[cache]` table the next builds run under."""
        (self.root / LOCAL_SETTINGS).write_text(toml(table))
        cache = cache_shim.settings({"cache": table}, self.root, LOCAL_SETTINGS)
        assert cache is not None
        self.cache = cache

    def tine(self, *arguments: str, echo: bool = True) -> str:
        """`tine buck` call in the round trip's isolation dir; echo and return its output."""
        command = [str(self.root / "bin" / "tine"), "buck", "--isolation-dir", ISOLATION, *arguments]
        print("+", " ".join(command[1:]), file=sys.stderr, flush=True)
        proc = subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if echo or proc.returncode != 0:
            print(proc.stdout, end="", file=sys.stderr, flush=True)
        if proc.returncode != 0:
            fail(f"tine buck {' '.join(arguments)} exited with {proc.returncode}")
        return proc.stdout

    def build(self) -> None:
        """Build every target named in SERVED."""
        # The Go and Rust examples build once per distribution. Without a chosen distribution, Buck
        # finds the examples incompatible and fails the build.
        self.tine("build", "-c", "tine.distribution=fedora", *SERVED)

    def check_uploads(self, allowed: bool) -> None:
        """Fail unless Buck's executor offers uploads exactly when the shim takes them.

        Buck2 reads no capability for this: it probes by uploading, so a reader whose platform still
        allows uploads warns about a refusal for every action it runs. The executor's own view of it
        is in the platform's providers.
        """
        wanted = f"cache_upload_behavior: {'Enabled' if allowed else 'Disabled'}"
        if wanted not in self.tine("audit", "providers", PLATFORM, echo=False):
            fail(f"the execution platform does not say {wanted!r}")

    def executors(self) -> dict[tuple[str, str], str]:
        """Where each action of the last build came from, by target and category."""
        found: dict[tuple[str, str], str] = {}
        for line in self.tine("log", "what-ran", "--format", "json").splitlines():
            if not line.startswith("{"):
                continue  # the launcher's own notes
            record = json.loads(line)
            if action := ACTION.fullmatch(cast(str, record["identity"])):
                found[(action["target"], action["category"])] = cast(str, record["reproducer"]["executor"])
        return found

    def check_served(self, expected: str) -> None:
        """Fail unless every action of SERVED came from `expected` in the last build."""
        ran = self.executors()
        wrong = [
            f"{target} ({category}) from {ran.get((target, category), 'nowhere')}"
            for target, categories in SERVED.items()
            for category in categories
            if ran.get((target, category)) != expected
        ]
        if wrong:
            fail(f"expected {expected} for each of: {', '.join(wrong)}")

    def report(self) -> None:
        """Print the shim's counters and fail on the ones a healthy round trip never touches."""
        report = cache_shim.ask(self.cache.dir)
        if report is None:
            fail(f"nothing serves {self.cache.dir} after a build that needed it")
        counts = cast(dict[str, int], report["counts"])
        print(f"shim counters: {counts}", file=sys.stderr, flush=True)
        if noisy := [f"{what} {counts[what]}" for what in QUIET if counts.get(what)]:
            fail(f"the shim reports trouble: {', '.join(noisy)}; see {cache_shim.log_path(self.cache)}")

    def stop_shim(self) -> None:
        """Stop the shim and wait until it is gone, so the next build starts one with an empty store."""
        report = cache_shim.ask(self.cache.dir)
        if report is None:
            return
        os.kill(cast(int, report["pid"]), signal.SIGTERM)
        deadline = time.monotonic() + 30
        while cache_shim.ask(self.cache.dir) is not None:
            if time.monotonic() > deadline:
                fail(f"the shim, pid {report['pid']}, did not stop")
            time.sleep(0.2)

    def teardown(self) -> None:
        self.stop_shim()
        # `clean` stops the isolation dir's daemon as well as emptying it.
        self.tine("clean")
        (self.root / LOCAL_SETTINGS).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--weed", type=Path, required=True, help="the SeaweedFS binary")
    arguments = parser.parse_args()

    root = Path(buck_output("root", "--kind", "project"))
    if (root / LOCAL_SETTINGS).exists():
        fail(f"{root / LOCAL_SETTINGS} exists, and this writes one of its own; move it away first")

    with ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tine-cache-roundtrip.")))
        weed = seaweed.Seaweed(arguments.weed, scratch, BUCKET)
        stack.callback(weed.close)
        # Create CA/leaf keys. No settings yet, so no shim is started.
        subprocess.run(
            [*buck2_argv("-v", "0", "run", "tine//remote_cache:test-ca"), "--", str(scratch)], check=True
        )
        run = RoundTrip(root, scratch, weed)
        stack.callback(run.teardown)

        print("=== cold build: nothing in the bucket", file=sys.stderr)
        run.build()
        run.check_served("Local")
        run.check_uploads(True)
        run.report()

        print("=== warm build: a cleaned checkout, a reader shim, an empty store", file=sys.stderr)
        run.tine("clean")
        run.stop_shim()
        shutil.rmtree(run.cache.dir)
        run.settle(run.reader)
        run.build()
        run.check_served("Cache")
        run.check_uploads(False)
        run.report()
        print("=== every result named in SERVED came back out of the bucket", file=sys.stderr)


if __name__ == "__main__":
    main()
