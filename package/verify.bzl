# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify a repository's packages as a closure selects them."""

load("//:specs.bzl", "specs")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load(":system.bzl", "PackageSystemInfo")

# What verifies one repository's packages for one consumer: the declared keys, the package system's
# verify program run in the consumer's box, and the repository's pinned metadata, for a system whose
# signatures travel in the database rather than the package.
Verifier = record(
    # What the verify spec names the keys by: the `keyring` built from them, or for a system that
    # builds none, the declared `keys` as they are and the `time` to judge them as of.
    keys = dict[str, typing.Any],
    verify = RunInfo,
    repository = Artifact,
)

def _build_keyring(
    actions: AnalysisActions,
    box: BoxInfo,
    system: PackageSystemInfo,
    keys: dict[str, typing.Any],
    time: str | None,
    keyring: Artifact,
    name: str,
) -> None:
    actions.run(
        cmd_args(
            box_run(box = box, exe = system.keyring),
            specs.args(actions, name + ".spec.json", {"keys": keys, "out": keyring.as_output(), "time": time}),
        ),
        category = "keyring",
        identifier = name,
    )

def _keyring_impl(ctx: AnalysisContext) -> list[Provider]:
    # Every consumer of a promised artifact would have to assert a content-based path; keep the plain one.
    keyring = ctx.actions.declare_output("keyring", dir = True, has_content_based_path = False)
    _build_keyring(
        ctx.actions,
        ctx.attrs.box[BoxInfo],
        ctx.attrs.package_system[PackageSystemInfo],
        ctx.attrs.keys,
        ctx.attrs.time,
        keyring,
        "keyring",
    )
    return [DefaultInfo(default_output = keyring)]

_keyring = anon_rule(
    impl = _keyring_impl,
    attrs = {
        "box": attrs.dep(providers = [BoxInfo]),
        "keys": attrs.dict(attrs.string(), attrs.source()),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "time": attrs.option(attrs.string(), default = None),
    },
    artifact_promise_mappings = {
        "keyring": lambda providers: providers[DefaultInfo].default_outputs[0],
    },
)

def repository_verifier(
    ctx: AnalysisContext,
    box: BoxInfo | Dependency,
    id: str,
    signing_keys: dict[str, Artifact | None],
    package_system: Dependency,
    directory: Artifact,
    keyrings: dict[str, Artifact],
    pinned_at: str | None = None,
) -> Verifier | None:
    """The verifier for repository `id`'s packages in a consumer that verifies with `box`.

    The keyring is built from the declared keys, any others are refused. None for a repository without
    declared keys. A system whose verify program reads key files as they are has no keyring driver, and
    is handed the declared ones.

    A `box` given as a dependency builds the keyring in an anonymous target, which every consumer that
    verifies with the same box and keys shares. Only the bootstrap root of a root box is no target and
    passes a BoxInfo. `keyrings` then caches the keyrings that the caller built so far, so repositories
    declaring the same keys share one keyring instead of declaring the same output twice.

    `pinned_at` is when the repository's snapshot was published: signatures are judged as of then,
    so a pinned snapshot verifies the same way however long after it is built.
    """
    if not signing_keys:
        return None
    system = package_system[PackageSystemInfo]
    if system.verify == None:
        fail("repository '{}': package system {} verifies no signatures".format(id, package_system.label))
    box_info = box[BoxInfo] if isinstance(box, Dependency) else box
    verify = box_run(box = box_info, exe = system.verify)
    if system.keyring == None:
        return Verifier(keys = {"keys": signing_keys, "time": pinned_at}, verify = verify, repository = directory)

    fingerprints = sorted(signing_keys)
    if isinstance(box, Dependency):
        keyring = ctx.actions.anon_target(
            _keyring,
            {
                "box": box,
                "keys": signing_keys,
                "name": "//keyring:" + "-".join([fingerprint[-8:] for fingerprint in fingerprints]),
                "package_system": package_system,
                "time": pinned_at,
            },
        ).artifact("keyring")
        return Verifier(keys = {"keyring": keyring}, verify = verify, repository = directory)

    key = str(package_system.label) + " " + str(pinned_at) + " " + " ".join(fingerprints)
    keyring = keyrings.get(key)
    if keyring == None:
        # Named by the keys' short ids: readable, and a collision between distinct sets declares the
        # same output twice, which fails loudly rather than sharing wrongly.
        name = "keyring-" + "-".join([fingerprint[-8:] for fingerprint in fingerprints])
        keyring = ctx.actions.declare_output(name, dir = True)
        _build_keyring(ctx.actions, box_info, system, signing_keys, pinned_at, keyring, name)
        keyrings[key] = keyring
    return Verifier(keys = {"keyring": keyring}, verify = verify, repository = directory)

def verify_packages(
    actions: AnalysisActions,
    name: str,
    id: str,
    verifier: Verifier,
    packages: dict[str, Artifact],
) -> Artifact:
    """Verify what closure `name` selects from repository `id`, publishing a directory of copies named by key."""
    out = actions.declare_output(name + "." + id + ".verified", dir = True)
    actions.run(
        cmd_args(
            verifier.verify,
            specs.args(
                actions,
                name + "." + id + ".verify.spec.json",
                {
                    "out": out.as_output(),
                    "packages": packages,
                    "repository": verifier.repository,
                }
                | verifier.keys,
            ),
        ),
        category = "verify",
        identifier = name + "/" + id,
    )
    return out
