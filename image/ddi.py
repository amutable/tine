#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Create independent partitions or a composed GPT image with systemd-repart."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self, TypedDict

import specs
import util

import finalize
import storage
import uapi16

# The algorithm of an erofs partition without `compression` in a compressed run, and its level when the
# partition sets no `compression_level` either.
_EROFS_COMPRESSION = ("zstd", "3")

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


def mkfs_environment(options: dict[str, list[str]], *, compressed_erofs: bool) -> dict[str, str]:
    """Return the systemd-repart environment that passes `options` to mkfs, per filesystem.

    `compressed_erofs` is set when every erofs partition of the run is compressed.
    """
    erofs = options.get("erofs", [])
    if compressed_erofs:
        # -C524288 and -Efragments,ztailpacking,dedupe make a compressed erofs partition substantially
        # smaller. The options of the caller come after them and override them. An uncompressed
        # partition must not get -Ededupe. With -Ededupe, mkfs.erofs switches to chunk-based
        # deduplication and reads the file data into a separate blob first. The partition then no longer
        # shares the data blocks of the image's files.
        erofs = ["-C524288", "-Efragments,ztailpacking,dedupe", *erofs]
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


class ExtensionSpec(TypedDict):
    # The number of leading lowers that form the parent image. Only the layers above them are packaged.
    parent: int
    # The seed derives from this target label, release and architecture, unless the spec gives a seed.
    identity: str
    name: str
    # The image paths of the extension-release files, one per class.
    releases: list[str]


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
    # Image directories whose contents the partitions leave out. The directories stay, empty.
    exclude: list[str]
    # Where to list what the composed disk carries, its partitions taken together.
    manifest: str | None
    # Filesystem -> the options mkfs is given for it.
    mkfs_options: dict[str, list[str]]
    # Whether compression is on. Then each partition is compressed as compressed() says.
    compress: bool
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
    compression_level: str | int | None
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
            compression_level=value["compression_level"],
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
        _setting(lines, "CompressionLevel", self.compression_level)
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


def _extension_seed(identity: str, name: str, releases: list[bytes]) -> uuid.UUID:
    """Derive the UUIDs of an extension from its name and the contents of its release files."""
    digest = hashlib.sha256(identity.encode())
    digest.update(b"\0name\0" + name.encode())
    for release in releases:
        digest.update(b"\0release\0" + release)
    return uuid.uuid5(_EXTENSION_SEED_NAMESPACE, digest.hexdigest())


def _add_directories(snapshot: uapi16.Snapshot, full: list[uapi16.Object], names: list[str]) -> None:
    """Add each top-level directory in `names` that `snapshot` lacks, with its metadata from `full`."""
    present = {obj.get("name") for obj in snapshot.objects}
    known = uapi16.by_name(full)
    added = [
        known.get(name, {"name": name, "type": "dir", "mode": 0o755, "mTimeNSec": full[0]["mTimeNSec"]})
        for name in names
        if name and name not in present
    ]
    snapshot.objects = [snapshot.objects[0]] + sorted(
        snapshot.objects[1:] + added, key=lambda obj: uapi16.order(obj["name"])
    )


def _exclude(snapshot: uapi16.Snapshot, directories: list[str]) -> None:
    """Remove the contents of each directory in `directories` from `snapshot`, and keep the directory."""
    excluded = [directory.strip("/") for directory in directories]
    snapshot.objects = [
        obj
        for obj in snapshot.objects
        if not any(obj.get("name", "").startswith(directory + "/") for directory in excluded)
    ]


def _listing(objects: list[uapi16.Object], roots: tuple[str, ...]) -> list[uapi16.Object]:
    """Return the manifest of the objects that a partition copying `roots` contains."""
    scope = uapi16.scope(roots)
    listed: list[uapi16.Object] = [{"mediaType": uapi16.MEDIA_TYPE}]
    for obj in objects[1:]:
        name = obj["name"]
        if scope != [""] and not any(name == root or name.startswith(root + "/") for root in scope):
            continue
        listed.append(obj.copy())
    uapi16.renumber(listed)
    return listed


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
        # repart only takes an absolute CopyBlocks= path.
        blocks = partition.blocks.absolute()
        (directory / filename).write_text(partition.definition.render_import(blocks, metadata))
        index += 1
    return files


def _partition_row(rows: list[dict[str, Any]], filename: str) -> dict[str, Any]:
    matches = [row for row in rows if Path(row.get("file", "")).name == filename]
    if len(matches) != 1:
        util.fail(f"ddi: expected one result for {filename}, found {len(matches)}")
    return matches[0]


def _move_partition(row: dict[str, Any], name: str, blocks: Path, metadata: Path) -> None:
    source_value = row.get("split_path")
    if not source_value or source_value == "-":
        util.fail(f"ddi: no split artifact was produced for {name}")
    source = Path(source_value)
    blocks.parent.mkdir(parents=True, exist_ok=True)
    source.replace(blocks)

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


def compressed(definition: Definition, compress: bool) -> Definition:
    """Return `definition` with the compression it gets in a run with `compress`.

    `compression = "none"` keeps a partition uncompressed in a compressed run.
    """
    if not compress or definition.compression == "none":
        return replace(definition, compression=None, compression_level=None)
    if definition.filesystem == "erofs" and definition.compression is None:
        algorithm, level = _EROFS_COMPRESSION
        return replace(
            definition, compression=algorithm, compression_level=definition.compression_level or level
        )
    return definition


def _compressed_erofs(written: list[WrittenPartition]) -> bool:
    """Return whether the run writes erofs partitions and compresses every one of them."""
    erofs = [partition.definition for partition in written if partition.definition.filesystem == "erofs"]
    return bool(erofs) and all(definition.compression is not None for definition in erofs)


def _repart(
    tree: Path,
    definitions: Path,
    image: Path,
    *,
    seed: uuid.UUID,
    signing: KeySpec | None,
    mkfs_options: dict[str, list[str]],
    compressed_erofs: bool,
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
        # With --root, repart opens CopyBlocks= paths below the tree, which doesn't contain the
        # imported partitions. --copy-source only applies to CopyFiles=.
        f"--copy-source={tree}",
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
    env = os.environ | mkfs_environment(mkfs_options, compressed_erofs=compressed_erofs)
    result = subprocess.run([*cmd, str(image)], check=True, env=env, stdout=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "ddi", argv)

    if not spec["definitions"] and not spec["partitions"]:
        util.fail("ddi: specify at least one definition or imported partition")
    raw_definitions = [entry["definition"] for entry in spec["definitions"]]
    written = [
        WrittenPartition(
            compressed(Definition.parse(entry["definition"]), spec["compress"]),
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

    extension = spec["extension"]
    snapshot = uapi16.Snapshot(spec["lower"])
    releases: list[bytes] = []
    if extension:
        full = snapshot.objects
        objects = uapi16.by_name(full)
        releases = [snapshot.content(objects[path]).read_bytes() for path in extension["releases"]]
        if extension["parent"]:
            storage.delta(snapshot, extension["parent"])
        # The delta lacks a directory of the parent that the extension doesn't change, /opt most
        # often. repart fails on a CopyFiles= source that doesn't exist.
        sources = [source.strip("/") for partition in written for source in partition.definition.sources]
        _add_directories(snapshot, full, sources)
    _exclude(snapshot, spec["exclude"])

    # What each partition ends up containing, listed from the snapshot that repart copies. A
    # definition takes the paths it names and nothing else, so a disk whose definitions are a /usr
    # partition and an ESP has no listing of /var, which no partition contains.
    listings = []
    for partition in written:
        if partition.manifest is not None:
            listed = _listing(snapshot.objects, partition.definition.sources)
            uapi16.write_listing(listed, partition.manifest)
            listings.append(partition.manifest)

    # An imported partition was listed by the invocation that filled it, and this one lists what it
    # copies itself, so between them they list the whole disk. Imported first: a partition this
    # invocation mounts over a directory another one contains is the one on top.
    if spec["manifest"]:
        imported = [it.manifest for it in partitions if it.manifest is not None]
        uapi16.merge(imported + listings, Path(spec["manifest"]))

    out = Path(spec["out"]) if spec["out"] else None
    # The disk and the split partitions are renamed from the scratch directory to the outputs. The
    # sandbox mounts the action's scratch space at TMPDIR. rename() fails with EXDEV between two
    # mounts, so the scratch directory goes next to an output instead of into TMPDIR.
    beside = out or splits[0][1]
    beside.parent.mkdir(parents=True, exist_ok=True)
    with (
        storage.root(snapshot, readonly=True) as restored,
        tempfile.TemporaryDirectory(prefix="repart.", dir=beside.parent) as scratch_dir,
    ):
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
        # A split artifact is named after the image repart writes, so the scratch file carries the
        # published basename even when only the partitions leave this invocation.
        disk = scratch / f"{spec['basename']}.raw" if splits else out
        assert disk is not None  # one of a disk output and split outputs is required
        rows = _repart(
            restored.path,
            repart_definitions,
            disk,
            seed=seed,
            signing=spec["signing"],
            mkfs_options=spec["mkfs_options"],
            compressed_erofs=_compressed_erofs(written),
            architecture=spec["architecture"],
            split=bool(splits),
        )

        for name, blocks, metadata in splits:
            _move_partition(_partition_row(rows, files[name]), name, blocks, metadata)
        if spec["root_hash_out"]:
            write_root_hash(rows, Path(spec["root_hash_out"]))
        if out and splits:
            disk.replace(out)

    artifacts = []
    if out:
        artifacts.append(out.name)
    if splits:
        artifacts.append(f"{len(splits)} partitions")
    mode = " and ".join(artifacts)
    print(f"ddi: wrote {mode} (seed={seed})", file=sys.stderr)


if __name__ == "__main__":
    main()
