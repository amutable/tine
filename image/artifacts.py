#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Extract named artifacts from a logical image."""

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pefile
import specs
import util

import finalize


class Spec(finalize.ImageSpec):
    # The boot driver's selection manifest and the artifact selected from it.
    manifest: str
    artifact: str
    out: str


@dataclass(frozen=True)
class Section:
    offset: int
    size: int


@dataclass(frozen=True)
class Source:
    path: str
    section: str | None = None


def pe_sections(path: Path) -> dict[str, Section]:
    """Return the base sections of a PE binary, by name.

    A UKI with profiles repeats sections such as `.cmdline` after each `.profile`, and systemd-stub
    applies a profile's on top of the base. What precedes the first `.profile` is what boots without
    one, so that is the only set read here.
    """
    try:
        with pefile.PE(str(path), fast_load=True) as image:
            sections: dict[str, Section] = {}
            file_size = path.stat().st_size
            for pe_section in image.sections:
                name = pe_section.Name.partition(b"\0")[0].decode("ascii", "strict")
                if name == ".profile":
                    break
                virtual_size = pe_section.Misc_VirtualSize
                raw_size = pe_section.SizeOfRawData
                offset = pe_section.PointerToRawData
                size = raw_size if virtual_size == 0 else min(virtual_size, raw_size)
                if offset + size > file_size:
                    util.fail(f"artifacts: PE section {name!r} exceeds {path}")
                sections[name] = Section(offset=offset, size=size)
            return sections
    except OSError, UnicodeDecodeError, ValueError, pefile.PEFormatError:
        return {}


def section_bytes(path: Path, section: Section) -> bytes:
    with path.open("rb") as stream:
        stream.seek(section.offset)
        value = stream.read(section.size)
    if len(value) != section.size:
        raise ValueError("truncated PE binary")
    return value


def section_text(path: Path, section: Section) -> str:
    try:
        return section_bytes(path, section).rstrip(b"\0\n").decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        util.fail(f"artifacts: invalid text section in {path}: {error}")


def _image_path(tree: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        util.fail(f"artifacts: invalid image path {value!r}")
    return tree.joinpath(*path.parts[1:])


def _read_source(manifest: Path, name: str) -> Source:
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or name not in value:
        util.fail(f"artifacts: manifest has no {name!r} artifact")
    source = value[name]
    if source is None:
        util.fail(f"artifacts: image has no {name!r} artifact")
    if not isinstance(source, dict) or set(source) != {"path", "section"}:
        util.fail(f"artifacts: invalid {name!r} artifact")
    path = source.get("path")
    section = source.get("section")
    if not isinstance(path, str) or (section is not None and not isinstance(section, str)):
        util.fail(f"artifacts: invalid {name!r} artifact")
    return Source(path, section)


def _copy_section(source: Path, section: Section, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, out.open("wb") as dst:
        src.seek(section.offset)
        remaining = section.size
        while remaining:
            chunk = src.read(min(remaining, 1024 * 1024))
            if not chunk:
                util.fail(f"artifacts: truncated section in {source}")
            dst.write(chunk)
            remaining -= len(chunk)


def _extract(tree: Path, source: Source, out: Path) -> None:
    path = _image_path(tree, source.path)
    if source.section is None:
        out.parent.mkdir(parents=True, exist_ok=True)
        util.clone_file(path, out)
        return
    section = pe_sections(path).get(source.section)
    if section is None:
        util.fail(f"artifacts: {path} has no {source.section} section")
    _copy_section(path, section, out)


def _extract_text(tree: Path, source: Source, out: Path) -> None:
    path = _image_path(tree, source.path)
    section = pe_sections(path).get(source.section) if source.section is not None else None
    if section is None:
        util.fail(f"artifacts: {path} has no {source.section} section")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(section_text(path, section) + "\n", encoding="utf-8")


# Artifacts that are text, written without the NUL terminator the PE section carries, so a consumer
# can read them as they are.
_TEXT_ARTIFACTS = {"cmdline"}


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "artifacts", argv)

    source = _read_source(Path(spec["manifest"]), spec["artifact"])
    with finalize.image(spec, program="artifacts") as tree:
        if spec["artifact"] in _TEXT_ARTIFACTS:
            _extract_text(tree, source, Path(spec["out"]))
        else:
            _extract(tree, source, Path(spec["out"]))


if __name__ == "__main__":
    main()
