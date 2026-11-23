# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The files of a project directory."""

# In a glob() pattern, `*` and `**` never match a name that starts with a dot. Only a pattern component
# such as `.*` matches that name. A pattern therefore matches a path only if the path contains exactly as
# many names that start with a dot as the pattern contains such components. all_files() uses two patterns
# for each count from 1 to _DOT_NAMES: one for a path that ends in such a name, and one for the files
# below it.
_DOT_NAMES = 4

def all_files(directory: str = "") -> list[str]:
    """Return the files below `directory` in this package with at most _DOT_NAMES names in their path that start with a dot."""
    prefix = directory + "/" if directory else ""
    patterns = []
    for count in range(_DOT_NAMES + 1):
        pattern = "**" + "".join(["/.*" if index == 0 else "/**/.*" for index in range(count)])
        patterns += [prefix + pattern] + ([prefix + pattern + "/**"] if count else [])
    return glob(patterns)
