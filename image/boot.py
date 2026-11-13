#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Select boot artifacts from a logical image."""

import functools
import json
from dataclasses import dataclass
from pathlib import Path

import specs
from util import fail

import artifacts
import finalize
import kmod
import version


class Spec(finalize.ImageSpec):
    out: str


@dataclass(frozen=True)
class Source:
    path: str
    section: str | None = None


@dataclass(frozen=True)
class Selection:
    kernel_release: str
    uki: Source | None
    kernel: Source
    initrd: Source
    cmdline: Source | None


@dataclass(frozen=True)
class UkiCandidate:
    kernel_release: str
    path: Path
    sections: dict[str, artifacts.Section]


@dataclass(frozen=True)
class KernelCandidate:
    kernel_release: str
    path: Path


def _image_name(tree: Path, path: Path) -> str:
    return "/" + path.relative_to(tree).as_posix()


def _candidate_compare(
    left: UkiCandidate | KernelCandidate,
    right: UkiCandidate | KernelCandidate,
) -> int:
    order = version.compare(left.kernel_release, right.kernel_release)
    if order:
        return order
    left_path = str(left.path)
    right_path = str(right.path)
    return (left_path > right_path) - (left_path < right_path)


def _ukis(tree: Path) -> list[UkiCandidate]:
    candidates = []
    for directory in (tree / "boot/EFI/Linux", tree / "efi/EFI/Linux"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.efi")):
            sections = artifacts.pe_sections(path)
            if ".linux" not in sections or ".uname" not in sections:
                continue
            kernel_release = artifacts.section_text(path, sections[".uname"])
            if kernel_release:
                candidates.append(UkiCandidate(kernel_release, path, sections))
    return candidates


def _kernels(tree: Path) -> list[KernelCandidate]:
    return [KernelCandidate(installed.release, installed.path) for installed in kmod.kernels(tree)]


def _matching_initrd(tree: Path, kernel_release: str) -> Path | None:
    candidates = (
        tree / f"boot/initramfs-{kernel_release}.img",
        tree / f"boot/initrd.img-{kernel_release}",
        tree / f"boot/initrd-{kernel_release}.img",
        tree / f"boot/initrd-{kernel_release}",
        tree / "usr/lib/modules" / kernel_release / "initrd",
    )
    return next((path for path in candidates if path.is_file()), None)


def _select(tree: Path) -> Selection:
    ukis = _ukis(tree)
    if ukis:
        uki = max(ukis, key=functools.cmp_to_key(_candidate_compare))
        initrd = Source(_image_name(tree, uki.path), ".initrd") if ".initrd" in uki.sections else None
        if initrd is None and (standalone := _matching_initrd(tree, uki.kernel_release)) is not None:
            initrd = Source(_image_name(tree, standalone))
        if initrd is None:
            fail(f"boot: selected kernel {uki.kernel_release} has no matching initrd")
        source = Source(_image_name(tree, uki.path))
        return Selection(
            kernel_release=uki.kernel_release,
            uki=source,
            kernel=Source(source.path, ".linux"),
            initrd=initrd,
            cmdline=Source(source.path, ".cmdline") if ".cmdline" in uki.sections else None,
        )

    kernels = _kernels(tree)
    if not kernels:
        fail("boot: image contains no UKI or standalone kernel")
    kernel = max(kernels, key=functools.cmp_to_key(_candidate_compare))
    initrd = _matching_initrd(tree, kernel.kernel_release)
    if initrd is None:
        fail(f"boot: selected kernel {kernel.kernel_release} has no matching initrd")
    return Selection(
        kernel_release=kernel.kernel_release,
        uki=None,
        kernel=Source(_image_name(tree, kernel.path)),
        initrd=Source(_image_name(tree, initrd)),
        cmdline=None,
    )


def _source_json(source: Source) -> dict[str, str | None]:
    return {"path": source.path, "section": source.section}


def _write_selection(selection: Selection, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "kernel_release": selection.kernel_release,
                "uki": _source_json(selection.uki) if selection.uki is not None else None,
                "kernel": _source_json(selection.kernel),
                "initrd": _source_json(selection.initrd),
                "cmdline": _source_json(selection.cmdline) if selection.cmdline is not None else None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "boot", argv)

    with finalize.image(spec, program="boot") as tree:
        _write_selection(_select(tree), Path(spec["out"]))


if __name__ == "__main__":
    main()
