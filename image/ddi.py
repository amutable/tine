#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Create independent partitions or a composed GPT image with systemd-repart."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, TypedDict

import specs
import util

import finalize
import manifest
import rootfs

_SEED_NAMESPACE = uuid.UUID("5af2de99-4f9f-4e0b-a04b-bde36b068c4f")

# Extensions derive their seed from a namespace and inputs of their own. Changing either changes
# the partition UUIDs of every published extension.
_EXTENSION_SEED_NAMESPACE = uuid.UUID("b388a973-3ffa-44aa-90b7-afcc4573ea9f")


class KeySpec(TypedDict):
    """One signing key as a driver-spec object; sign.bzl's signing_key_spec() produces it."""

    private_key: str
    certificate: str
    # OpenSSL sources in systemd's spelling, each None for material in the build graph.
    private_key_source: str | None
    certificate_source: str | None


def _key_arguments(signing: KeySpec | None) -> list[str]:
    """systemd-repart options selecting the key that signs a verity root hash."""
    if signing is None:
        return []
    arguments = ["--private-key", signing["private_key"], "--certificate", signing["certificate"]]
    if signing["private_key_source"]:
        arguments += ["--private-key-source", signing["private_key_source"]]
    if signing["certificate_source"]:
        arguments += ["--certificate-source", signing["certificate_source"]]
    return arguments


def mkfs_environment(options: dict[str, list[str]]) -> dict[str, str]:
    """Return the systemd-repart environment that passes `options` to mkfs, per filesystem."""
    erofs = options.get("erofs", [])
    # The kernel shares the page cache of identical files between erofs images mounted with
    # inode_share, but only for images that record a digest of each file.
    if not any(option.startswith("--xattr-inode-digest") for option in erofs):
        erofs = [*erofs, "--xattr-inode-digest"]
    # repart reads one variable per filesystem it formats and splits each on whitespace.
    return {
        f"SYSTEMD_REPART_MKFS_OPTIONS_{filesystem.upper()}": " ".join(arguments)
        for filesystem, arguments in (options | {"erofs": erofs}).items()
    }


class WrittenPartitionSpec(TypedDict):
    definition: dict[str, Any]
    # Where to write it as a standalone artifact and the metadata describing it, for an invocation
    # that splits its partitions back out; absent for one that only composes them into a disk.
    blocks: str | None
    metadata: str | None
    # Where to list what it carries; absent for a partition that copies nothing, so there is
    # nothing to list: a verity partition holds a hash tree, and a filesystem created empty is
    # populated by the system that grows it rather than by this build.
    manifest: str | None


class ImportedPartitionSpec(TypedDict):
    definition: dict[str, Any]
    # Where it already is, written by the invocation that filled it, and the metadata describing it.
    blocks: str
    metadata: str
    # What it carries, listed by that same invocation; absent for the same reason.
    manifest: str | None


class Release(TypedDict):
    # The path of the extension-release file, relative to the image root.
    path: str
    # The driver writes the fields in this order.
    fields: dict[str, str]


class ExtensionSpec(TypedDict):
    # The number of leading lowers that form the parent image. Only the layers above them are packaged.
    parent: int
    # The seed derives from this target label, unless the spec gives a seed.
    identity: str
    name: str
    # An extension has one extension-release file per class.
    releases: list[Release]


class Spec(finalize.ImageSpec):
    out: str | None
    # The systemd spelling of the architecture the image runs on. repart picks the partition types of
    # root and usr partitions for it. With None, repart uses the build host's types.
    architecture: str | None
    # What the artifacts of this invocation are named after, extension aside.
    basename: str
    # Stable target identity the seed derives from, unless one is given outright.
    identity: str
    seed: str | None
    signing: KeySpec | None
    # The partitions this invocation writes, and the independent ones copied into the result.
    definitions: list[WrittenPartitionSpec]
    partitions: list[ImportedPartitionSpec]
    root_hash_out: str | None
    # Image paths holding the package database, stripped from the partitions.
    pkgdb_paths: list[str]
    # Image directories whose contents the partitions leave out. The directories stay, empty.
    exclude: list[str]
    # Where to list what the composed disk carries, its partitions taken together.
    manifest: str | None
    # Filesystem -> the options mkfs is given for it.
    mkfs_options: dict[str, list[str]]
    # The driver prepares the tree of an extension before repart copies it.
    extension: ExtensionSpec | None


@dataclass(frozen=True)
class Definition:
    name: str
    type: str
    label: str | None
    filesystem: str | None
    copy_files: tuple[str, ...]
    size_min: str | int | None
    size_max: str | int | None
    minimize: str | None
    compression: str | None
    verity: str | None
    verity_match_key: str | None

    @classmethod
    def parse(cls, value: dict[str, Any]) -> Self:
        return cls(
            name=value["name"],
            type=value["type"],
            label=value["label"],
            filesystem=value["filesystem"],
            copy_files=tuple(value["copy_files"]),
            size_min=value["size_min"],
            size_max=value["size_max"],
            minimize=value["minimize"],
            compression=value["compression"],
            verity=value["verity"],
            verity_match_key=value["verity_match_key"],
        )

    @property
    def sources(self) -> tuple[str, ...]:
        """The image paths this partition is populated from, whatever it is they are copied to."""
        return tuple(copy.split(":", 1)[0] for copy in self.copy_files)

    def render(self, *, split: bool) -> str:
        lines = ["[Partition]", f"Type={self.type}"]
        _setting(lines, "Label", self.label)
        _setting(lines, "Format", self.filesystem)
        _setting(lines, "SizeMinBytes", self.size_min)
        _setting(lines, "SizeMaxBytes", self.size_max)
        _setting(lines, "Minimize", self.minimize)
        _setting(lines, "Compression", self.compression)
        _setting(lines, "Verity", self.verity)
        _setting(lines, "VerityMatchKey", self.verity_match_key)
        for copy in self.copy_files:
            lines.append(f"CopyFiles={copy}")
        if split:
            # repart resolves this against the name of the image it writes, so a split artifact
            # leaves the run already named the way it is published: %t is the partition type and
            # %U the UUID it generated, which systemd-sysupdate reads back out of the name as @u
            # to give the partition it writes that same UUID.
            lines.append("SplitName=%t.%U")
        return "\n".join(lines) + "\n"

    def render_import(self, blocks: Path, metadata: dict[str, Any]) -> str:
        """Preserve the partition's identity while replacing population with CopyBlocks."""
        lines = [
            "[Partition]",
            f"Type={metadata['type']}",
            f"UUID={metadata['uuid']}",
        ]
        _setting(lines, "Label", metadata.get("label"))
        _setting(lines, "SizeMinBytes", self.size_min)
        _setting(lines, "SizeMaxBytes", self.size_max)
        _setting(lines, "Verity", self.verity)
        _setting(lines, "VerityMatchKey", self.verity_match_key)
        lines.append(f"CopyBlocks={blocks}")

        # An imported partition already exists as a standalone artifact, so splitting it back out
        # would write a second copy of it and discard that. SplitName= otherwise defaults to %t.
        lines.append("SplitName=-")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class WrittenPartition:
    definition: Definition
    blocks: Path | None
    metadata: Path | None
    manifest: Path | None


@dataclass(frozen=True)
class ImportedPartition:
    definition: Definition
    blocks: Path
    metadata: Path
    manifest: Path | None


def _setting(lines: list[str], name: str, value: object | None) -> None:
    if value is not None:
        lines.append(f"{name}={value}")


def _derived_seed(
    identity: str,
    definitions: list[dict[str, Any]],
    partitions: list[ImportedPartition],
) -> uuid.UUID:
    """Derive UUIDs from logical configuration, not action-local CopyBlocks paths.

    A definition is re-encoded exactly as the rule spelled it, key order included (see
    ddi.bzl:partition): any other encoding re-identifies every partition of every
    unchanged image.
    """
    digest = hashlib.sha256(identity.encode())
    for definition in definitions:
        digest.update(b"\0definition\0" + json.dumps(definition, separators=(",", ":")).encode())
    for partition in partitions:
        digest.update(b"\0partition\0" + partition.definition.name.encode() + b"\0")
        digest.update(partition.metadata.read_bytes())
    return uuid.uuid5(_SEED_NAMESPACE, digest.hexdigest())


def _extension_seed(identity: str, name: str, releases: list[Release]) -> uuid.UUID:
    """Derive the UUIDs of an extension from its name and release fields."""
    digest = hashlib.sha256(identity.encode())
    digest.update(b"\0name\0" + name.encode())
    for release in releases:
        for key, value in release["fields"].items():
            digest.update(b"\0release\0" + key.encode() + b"=" + value.encode())
    return uuid.uuid5(_EXTENSION_SEED_NAMESPACE, digest.hexdigest())


def _os_release(tree: Path) -> dict[str, str]:
    """Parse the parent image's os-release for strict extension matching."""
    for candidate in ("usr/lib/os-release", "etc/os-release"):
        path = tree / candidate
        if not path.is_file():
            continue
        fields = {}
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if key and sep and not key.startswith("#"):
                fields[key] = value.strip("\"'")
        return fields
    util.fail("ddi: the parent image ships no os-release to match against")


def pin_release(parent: dict[str, str], release: dict[str, str]) -> dict[str, str]:
    """Add the parent's identity to the extension-release fields `release` does not set.

    The extension is a delta against the parent it was built on, so systemd must refuse to merge
    it onto anything else: it pins the parent's ID and VERSION_ID, and the IMAGE_ID and
    IMAGE_VERSION of a parent that is an image build. An extension that declares ID=_any matches
    any OS and gets none of them.
    """
    if release.get("ID") == "_any":
        return release
    keys = ("ID", "VERSION_ID", "IMAGE_ID", "IMAGE_VERSION")
    strict = {key: parent[key] for key in keys if key in parent and key not in release}
    return strict | release


def _extension_layers(spec: Spec, extension: ExtensionSpec) -> tuple[list[Release], list[str | Path]]:
    """Return the extension-release files and the layers the extension packages.

    With a parent, the fields are pinned to the parent's os-release and the layers are the delta
    above it. Without a parent, the function returns the releases and layers of the spec unchanged.
    """
    releases = extension["releases"]
    parent = extension["parent"]
    lowers: list[str | Path] = list(spec["lower"])
    if not parent:
        return releases, lowers
    with rootfs.rootfs("/buildroot", lowers=lowers) as tree:
        fields = _os_release(tree)
    if "ID" not in fields:
        util.fail("ddi: the parent os-release lacks ID")
    pinned = [
        Release(path=release["path"], fields=pin_release(fields, release["fields"])) for release in releases
    ]
    return pinned, lowers[parent:]


def empty_directories(tree: Path, directories: list[str]) -> None:
    """Remove the contents of each directory in `directories` below `tree`, and keep the directory."""
    for relative in directories:
        directory = tree / relative.lstrip("/")
        if directory.is_dir() and not directory.is_symlink():
            for child in directory.iterdir():
                util.remove_path(child)


def _write_definitions(
    directory: Path,
    written: list[WrittenPartition],
    partitions: list[ImportedPartition],
    *,
    split: bool,
) -> dict[str, str]:
    files = {}
    index = 0
    for partition in written:
        filename = f"{index:04}-{partition.definition.name}.conf"
        (directory / filename).write_text(partition.definition.render(split=split))
        files[partition.definition.name] = filename
        index += 1
    for partition in partitions:
        metadata: dict[str, Any] = json.loads(partition.metadata.read_text())
        filename = f"{index:04}-{partition.definition.name}.conf"
        copy_path = Path(f"/run/tine/repart/{index}.raw")
        (directory / filename).write_text(partition.definition.render_import(copy_path, metadata))
        index += 1
    return files


def _partition_row(rows: list[dict[str, Any]], filename: str) -> dict[str, Any]:
    matches = [row for row in rows if Path(row.get("file", "")).name == filename]
    if len(matches) != 1:
        util.fail(f"ddi: expected one result for {filename}, found {len(matches)}")
    return matches[0]


def _copy_partition(row: dict[str, Any], name: str, blocks: Path, metadata: Path) -> None:
    source_value = row.get("split_path")
    if not source_value or source_value == "-":
        util.fail(f"ddi: no split artifact was produced for {name}")
    source = Path(source_value)
    blocks.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, blocks)

    # systemd strips padding from signature split files. CopyBlocks requires a
    # sector-aligned artifact, so restore the partition's declared raw size.
    raw_size = int(row["raw_size"])
    if blocks.stat().st_size > raw_size:
        util.fail(f"ddi: split artifact for {name} exceeds its partition")
    with blocks.open("r+b") as f:
        f.truncate(raw_size)

    described = {
        "label": row.get("label"),
        "published": source.name,
        "raw_size": raw_size,
        "type": row["type"],
        "uuid": row["uuid"],
    }
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps(described, sort_keys=True, separators=(",", ":")) + "\n")


def write_root_hash(rows: list[dict[str, Any]], output: Path) -> None:
    """Write the verity root hash repart generated, as hex plus a newline.

    The rows are repart's --json output.
    """
    # repart reports the root hash TBD for a partition that it did not generate.
    hashes = {value for row in rows if (value := row.get("roothash")) not in (None, "TBD")}
    if len(hashes) != 1:
        util.fail(f"ddi: expected one generated verity root hash, found {len(hashes)}")
    output.write_text(hashes.pop() + "\n")


def _repart(
    tree: Path,
    definitions: Path,
    image: Path,
    *,
    seed: uuid.UUID,
    signing: KeySpec | None,
    mkfs_options: dict[str, list[str]],
    architecture: str | None,
    split: bool = False,
) -> list[dict[str, Any]]:
    """Create `image` from `tree` with the repart.d files in `definitions`, and return repart's report."""
    cmd = [
        "systemd-repart",
        "--empty=create",
        "--size=auto",
        "--dry-run=no",
        "--json=pretty",
        "--no-pager",
        f"--root={tree}",
        "--offline=yes",
        "--seed",
        str(seed),
        "--definitions",
        str(definitions),
    ]
    if architecture is not None:
        cmd.append(f"--architecture={architecture}")
    if split:
        cmd.append("--split=yes")
    cmd += _key_arguments(signing)
    env = os.environ | mkfs_environment(mkfs_options)
    result = subprocess.run([*cmd, str(image)], check=True, env=env, stdout=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "ddi", argv)

    if not spec["definitions"] and not spec["partitions"]:
        util.fail("ddi: specify at least one definition or imported partition")
    raw_definitions = [entry["definition"] for entry in spec["definitions"]]
    written = [
        WrittenPartition(
            Definition.parse(entry["definition"]),
            Path(entry["blocks"]) if entry["blocks"] else None,
            Path(entry["metadata"]) if entry["metadata"] else None,
            Path(entry["manifest"]) if entry["manifest"] else None,
        )
        for entry in spec["definitions"]
    ]
    partitions = [
        ImportedPartition(
            Definition.parse(partition["definition"]),
            Path(partition["blocks"]),
            Path(partition["metadata"]),
            Path(partition["manifest"]) if partition["manifest"] else None,
        )
        for partition in spec["partitions"]
    ]
    # A partition named as its own artifact is one repart splits back out, which is a property of
    # the invocation rather than of any one partition: it splits all of them or none.
    splits = [
        (partition.definition.name, partition.blocks, partition.metadata)
        for partition in written
        if partition.blocks is not None and partition.metadata is not None
    ]
    if not spec["out"] and not splits:
        util.fail("ddi: specify a disk output, split outputs, or both")

    binds: list[tuple[str | Path, str | Path]] = [
        (partition.blocks, f"/run/tine/repart/{index}.raw")
        for index, partition in enumerate(partitions, start=len(written))
    ]
    extension = spec["extension"]
    releases: list[Release] = []
    lowers = None
    if extension:
        releases, lowers = _extension_layers(spec, extension)
    with (
        finalize.image(spec, program="ddi", lowers=lowers, binds=binds) as tree,
        tempfile.TemporaryDirectory(prefix="repart.") as scratch_dir,
    ):
        if extension:
            # An extension identifies itself through its extension-release files. An os-release in
            # the extension would replace the os-release of the parent image in the merged tree.
            for path in ("usr/lib/os-release", "etc/os-release"):
                (tree / path).unlink(missing_ok=True)
        # The package database is a supply-chain artifact that the image's `[pkgdb]` and `[sbom]`
        # subtargets capture from the tree, so partitions nothing resolves packages in can drop it.
        for relative in spec["pkgdb_paths"]:
            util.remove_path(tree / relative, with_parents=True)
        empty_directories(tree, spec["exclude"])

        if extension:
            # A delta may lack a directory the root partition copies, /opt most often. Create it,
            # so that the DDI contains every directory of its classes.
            for partition in written:
                for source in partition.definition.sources:
                    if source.strip("/"):
                        (tree / source.strip("/")).mkdir(exist_ok=True)
            for release in releases:
                path = tree / release["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(f"{key}={value}\n" for key, value in release["fields"].items()))

        # What each partition ends up carrying, listed from the tree repart is about to copy. A
        # definition takes the paths it names and nothing else, so a disk whose definitions are a
        # /usr partition and an ESP has no listing of /var, which lands nowhere.
        epoch = int(os.environ["SOURCE_DATE_EPOCH"])
        listings = []
        for partition in written:
            if partition.manifest is not None:
                manifest.write(tree, partition.manifest, epoch, roots=partition.definition.sources)
                listings.append(partition.manifest)

        # An imported partition was listed by the invocation that filled it, and this one holds
        # what it copies itself, so between them they list the whole disk. Imported first: a
        # partition this invocation mounts over a directory another one holds is the one on top.
        if spec["manifest"]:
            imported = [it.manifest for it in partitions if it.manifest is not None]
            manifest.merge(imported + listings, Path(spec["manifest"]))

        scratch = Path(scratch_dir)
        repart_definitions = scratch / "repart.d"
        repart_definitions.mkdir()
        files = _write_definitions(
            repart_definitions,
            written,
            partitions,
            split=bool(splits),
        )

        if spec["seed"] is not None:
            seed = uuid.UUID(spec["seed"])
        elif extension:
            seed = _extension_seed(extension["identity"], extension["name"], releases)
        else:
            seed = _derived_seed(spec["identity"], raw_definitions, partitions)
        out = Path(spec["out"]) if spec["out"] else None
        # A split artifact is named after the image repart writes, so the scratch file carries the
        # published basename even when only the partitions leave this invocation.
        disk = scratch / f"{spec['basename']}.raw" if splits else out
        assert disk is not None  # one of a disk output and split outputs is required
        rows = _repart(
            tree,
            repart_definitions,
            disk,
            seed=seed,
            signing=spec["signing"],
            mkfs_options=spec["mkfs_options"],
            architecture=spec["architecture"],
            split=bool(splits),
        )

        for name, blocks, metadata in splits:
            _copy_partition(_partition_row(rows, files[name]), name, blocks, metadata)
        if spec["root_hash_out"]:
            write_root_hash(rows, Path(spec["root_hash_out"]))
        if out and splits:
            shutil.copyfile(disk, out)

    artifacts = []
    if out:
        artifacts.append(out.name)
    if splits:
        artifacts.append(f"{len(splits)} partitions")
    mode = " and ".join(artifacts)
    print(f"ddi: wrote {mode} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
