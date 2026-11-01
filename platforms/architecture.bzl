# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The CPU architectures tine builds for, and how each consumer spells them.

The canonical names are systemd's `ConditionArchitecture=` spellings, but with `_` instead of `-`.
Everything with its own naming schema reads it from this table:

- `config`: the Buck2 prelude's constraint value, which `select()` keys on
- `ec2`: EC2's AMI `Architecture`
- `efi`: ukify's `--efi-arch`, which also names the boot stubs and systemd-boot
- `go`: Go's `GOARCH`
- `systemd`: the `%a` specifier, which names published artifacts
- `rpm`, `pacman`, `deb`: the package system's own name, in its metadata and its mirrors' layout
- `rust`: the Rust target triple, which cargo's `--target` takes
"""

ARCHITECTURES = {
    "arm64": struct(
        config = "config//cpu:arm64",
        deb = "arm64",
        ec2 = "arm64",
        efi = "aa64",
        go = "arm64",
        pacman = "aarch64",
        rpm = "aarch64",
        rust = "aarch64-unknown-linux-gnu",
        systemd = "arm64",
    ),
    "x86_64": struct(
        config = "config//cpu:x86_64",
        deb = "amd64",
        ec2 = "x86_64",
        efi = "x64",
        go = "amd64",
        pacman = "x86_64",
        rpm = "x86_64",
        rust = "x86_64-unknown-linux-gnu",
        systemd = "x86-64",
    ),
}

def _check(names: list[str]) -> None:
    unknown = sorted([name for name in names if name not in ARCHITECTURES])
    if unknown:
        fail("architecture: unknown {}; tine builds for {}".format(unknown, sorted(ARCHITECTURES)))

def _select(values: dict[str, typing.Any]) -> Select:
    """select() the value declared for the configured architecture."""
    _check(values.keys())
    return select({ARCHITECTURES[name].config: value for name, value in values.items()})

def _spelling(name: str, schema: str) -> str:
    """How `schema`, a field of the table, spells the architecture `name`."""
    _check([name])
    spelled = getattr(ARCHITECTURES[name], schema)
    if spelled == None:
        fail("architecture: {} has no {} name".format(name, schema))
    return spelled

def _configured() -> Select:
    """A rule's attribute default for the architecture being built for.

    Under the target configuration (plain `dep`) this is the host's, unless `--target-platforms` picks
    another. Under the execution configuration (`exec_dep`), it is always the host's.
    """
    return _select({name: name for name in ARCHITECTURES})

def _host() -> str:
    """The architecture of the machine Buck runs on."""
    arch = host_info().arch
    if arch.is_aarch64:
        return "arm64"
    if arch.is_x86_64:
        return "x86_64"
    fail("architecture: tine runs on {}, not on this host".format(sorted(ARCHITECTURES)))

architecture = struct(
    ARCHITECTURES = ARCHITECTURES,
    configured = _configured,
    host = _host,
    select = _select,
    spelling = _spelling,
)
