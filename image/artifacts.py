# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read the sections of a PE binary, such as the kernel and initrd a UKI carries."""

from dataclasses import dataclass
from pathlib import Path

import pefile
import util


@dataclass(frozen=True)
class Section:
    offset: int
    size: int


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


def copy_section(source: Path, section: Section, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, out.open("wb") as dst:
        util.copy_range(dst.fileno(), 0, src.fileno(), section.offset, section.size)
