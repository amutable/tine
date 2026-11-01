# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Declaring a distribution, and pointing a target at one.

A distribution is one value of the `tine//distribution:distribution` constraint, the value of its
`tine//distribution:family` constraint, and the incoming transition that sets the two constraints.
The catalog declares one distribution per release in its root package, so the release name
`fedora.rawhide` names the label `catalog//:fedora.rawhide.distribution`.

The transition sets the constraint only where nothing has set it yet. The target being built is
therefore the one that decides, and every image below it, `parent` chain included, follows: a base
layer is not a distribution, it is whatever the leaf pulling it in is. For an architecture the
distribution does not serve, the transition leaves the constraint unset.
"""

load("@catalog//:releases.bzl", _CATALOG_RELEASES = "RELEASES")
load("@prelude//:rules.bzl", "constraint_value")
load("@prelude//cfg/exec_platform:marker.bzl", "get_exec_platform_marker")
load("//box:runtime.bzl", "BoxInfo")
load("//platforms:architecture.bzl", "architecture")
load("//platforms:defs.bzl", "platform")

# The families that tine declares releases for. Each one is a value of `tine//distribution:family`.
FAMILIES = ["arch", "debian", "fedora"]

# A rule that runs in the tools box of its release takes the box as a plain dependency rather than an
# execution one. The tools box then takes the distribution that the rule is configured for, and its
# incoming transition configures it for the host.
TOOLS_BOX_ATTR = attrs.dep(providers = [BoxInfo], default = "tine//distribution:tools.box")

def _distribution_impl(ctx: AnalysisContext) -> list[Provider]:
    value = ctx.attrs.constraint[ConstraintValueInfo]
    family_value = ctx.attrs.family[ConstraintValueInfo]
    own = {value.setting.label: value, family_value.setting.label: family_value}
    served = [cpu.label for configuration in ctx.attrs.architectures for cpu in configuration[ConfigurationInfo].constraints.values()]

    def served_on(constraints: dict[TargetLabel, ConstraintValueInfo]) -> bool:
        return bool([cpu for cpu in constraints.values() if cpu.label in served])

    def select_distribution(platform: PlatformInfo) -> PlatformInfo:
        constraints = platform.configuration.constraints

        # A distribution has no packages for an architecture it does not serve. With the
        # distribution unset, `compatibility()` makes every target under the transition incompatible.
        # A build of `//...` then skips such a target instead of failing on the missing packages.
        if value.setting.label in constraints or not served_on(constraints):
            return platform
        constraints.update(own)
        return PlatformInfo(
            label = platform.label,
            configuration = ConfigurationInfo(
                constraints = constraints,
                values = platform.configuration.values,
            ),
        )

    # One target answers every question asked of a distribution: it is the key a `select()`
    # branches on, the transition that puts it there, and a platform to build against.
    # The configuration contains only the distribution and family constraints. A `select()` key of
    # the distribution therefore matches whatever else the platform contains. The key also refines the
    # key of its family, so Buck picks the distribution's branch when a `select()` has a key for the
    # distribution and a key for its family.
    # The platform adds the distribution and family constraints to the base platform. Choosing a
    # distribution therefore keeps the cpu and os of the base platform. A base cpu the distribution
    # does not serve gets neither constraint, as under the transition.
    base = ctx.attrs.base[PlatformInfo].configuration
    return [
        DefaultInfo(),
        ConfigurationInfo(constraints = own, values = {}),
        TransitionInfo(impl = select_distribution),
        PlatformInfo(
            label = str(ctx.label.raw_target()),
            configuration = ConfigurationInfo(
                constraints = base.constraints | (own if served_on(base.constraints) else {}),
                values = base.values,
            ),
        ),
    ]

_distribution = rule(
    impl = _distribution_impl,
    attrs = {
        "architectures": attrs.list(attrs.dep(providers = [ConfigurationInfo]), doc = "the cpu configuration of each architecture the distribution serves"),
        "base": attrs.dep(providers = [PlatformInfo], default = "tine//platforms:default"),
        "constraint": attrs.dep(providers = [ConstraintValueInfo]),
        "family": attrs.dep(providers = [ConstraintValueInfo]),
    },
    is_configuration_rule = True,
)

def _box_host_impl(ctx: AnalysisContext) -> list[Provider]:
    base = ctx.attrs.base[PlatformInfo].configuration
    fixed = ctx.attrs.distribution[ConfigurationInfo].constraints if ctx.attrs.distribution != None else None
    default = ctx.attrs.default[ConfigurationInfo].constraints
    marker = ctx.attrs._exec_marker[ConstraintValueInfo]
    label = str(ctx.label.raw_target())

    # Buck calls a transition with the keyword argument `platform`, so `configure` needs a parameter
    # of that name. Inside `configure`, `platform` then refers to the parameter, not to the namespace.
    check_unmarked = platform.check_unmarked

    def configure(platform: PlatformInfo) -> PlatformInfo:
        check_unmarked(platform, marker)
        chosen = fixed
        if chosen == None:
            chosen = {setting: value for setting, value in platform.configuration.constraints.items() if setting in default} or default
        return PlatformInfo(
            label = label,
            configuration = ConfigurationInfo(constraints = base.constraints | chosen, values = base.values),
        )

    return [DefaultInfo(), TransitionInfo(impl = configure)]

# A box takes a `box_host` target as its incoming transition. The transition replaces the caller's
# configuration with `base` and a distribution. Every caller therefore reaches the same configured box,
# and a `select()` in the box's attributes sees that distribution. With `distribution`, the box is always
# configured for that distribution. Without it, the box is configured for the distribution of its caller.
# A caller without a distribution, such as an `exec_dep` or a target built by its own name, gets `default`.
# The execution platform registers no exec marker constraint, so the execution configuration is the
# same as the host's. An `exec_dep` therefore reaches the same configured box as an ordinary
# dependency. The host platform assumes one execution platform, for the host's architecture. A
# project can register an execution platform for another architecture. An `exec_dep` resolved for
# that platform still reaches the box built for the host. The actions on that platform then run
# binaries for the wrong architecture, and Buck reports no error.
box_host = rule(
    impl = _box_host_impl,
    attrs = {
        "base": attrs.dep(providers = [PlatformInfo], default = "tine//platforms:default"),
        "default": attrs.dep(providers = [ConfigurationInfo], default = "catalog//:default.distribution"),
        "distribution": attrs.option(attrs.dep(providers = [ConfigurationInfo]), default = None),
        "_exec_marker": attrs.dep(providers = [ConstraintValueInfo], default = get_exec_platform_marker()),
    },
    is_configuration_rule = True,
)

def new(name: str, architectures: list[str], visibility: list[str] | None = None) -> None:
    """Declare a distribution of `architectures` in the family that its name starts with, and the constraint value `select()` keys on.

    A box of the distribution's release is configured for `<name>.host`, the host platform with the
    distribution's constraints. A lock of the box for an architecture is configured for
    `<name>.<architecture>`.
    """
    if not name.endswith(".distribution"):
        fail("distribution name must end with '.distribution': {}".format(name))
    family = name.partition(".")[0]
    if family not in FAMILIES:
        fail("distribution {}: unknown family {!r}; tine knows {}".format(name, family, FAMILIES))
    # `distribution.select()` and `distribution.set_for_package()` turn a release name such as
    # `fedora.43` into the label `catalog//:fedora.43.distribution`. A release name can't refer to a
    # distribution declared anywhere else.
    if get_cell_name() != "catalog" or package_name() != "":
        fail("distribution {}: declare it in the root package of the catalog cell, not in {}//{}".format(name, get_cell_name(), package_name()))
    constraint_value(
        name = name + ".constraint",
        constraint_setting = "tine//distribution:distribution",
        visibility = visibility,
    )
    configurations = [architecture.spelling(arch, "config") for arch in architectures]
    _distribution(
        name = name,
        # The release's box is built for the host and exists only for `architectures`. A host of
        # another architecture has no box to install the release's packages with, so the distribution
        # serves no architecture there.
        architectures = configurations if architecture.host() in architectures else [],
        constraint = ":" + name + ".constraint",
        family = "tine//distribution:" + family,
        visibility = visibility,
    )
    box_host(
        name = name + ".host",
        distribution = ":" + name,
        visibility = visibility,
    )
    for arch in architectures:
        box_host(
            name = "{}.{}".format(name, arch),
            base = "tine//platforms:" + arch,
            distribution = ":" + name,
            visibility = visibility,
        )

    # A catalog that lists one release in releases.bzl has no other release to make the default. The
    # release macros declare `<release>.release` after the distribution, so rule_exists() can't check
    # the release that tine.box names here.
    release = name.removesuffix(".distribution")
    if _CATALOG_RELEASES.values() == [release]:
        _alias_default(read_root_config("tine", "box", release), ["PUBLIC"])

def default_release(release: str, visibility: list[str] = ["PUBLIC"]) -> None:
    """Declare a catalog's `default.*` targets as aliases of the targets of `release`, such as `fedora.rawhide`.

    tine's own boxes build from `default.release`. The `box` key of the `tine` buckconfig section names a
    release that the `default.*` targets point to instead of `release`. A catalog whose `releases.bzl`
    lists one release gets its `default.*` targets from `distribution.new()` and doesn't call this.
    """
    if len(_CATALOG_RELEASES) == 1:
        fail("default_release(): catalog//:releases.bzl lists one release, and the release declares the default.* targets itself; remove the call")
    release = read_root_config("tine", "box", release)
    if not rule_exists(release + ".release"):
        fail("tine.box: the catalog declares no release {!r}".format(release))
    _alias_default(release, visibility)

def _alias_default(release: str, visibility: list[str]) -> None:
    # box.new() derives the distribution's platforms from the release's name, so every derived target
    # needs an alias. tine//distribution:tools.box builds on `default.box` for a release that
    # catalog//:releases.bzl doesn't list.
    for role in ["release", "box", "distribution", "distribution.host"]:
        native.alias(
            name = "default." + role,
            actual = ":{}.{}".format(release, role),
            visibility = visibility,
        )

_DISTRIBUTIONS = "tine.distributions"

def _release(release: str, role: str) -> str:
    """Return the label of the target `role` of a catalog release, such as `fedora.rawhide`."""
    family, _, version = release.partition(".")
    if family not in FAMILIES or not version:
        fail("{!r} is not a release; a release is named `<family>.<version>`, and the families are {}".format(release, FAMILIES))
    return "catalog//:{}.{}".format(release, role)

def _describe(name: str, described: dict[str, str]) -> dict[str, str]:
    if "release" not in described:
        fail("distribution {} names no release of the catalog, such as `fedora.rawhide`: {}".format(name, described))
    if "distribution" in described:
        fail("distribution {}: the release names the distribution, so `distribution` cannot be set: {}".format(name, described))
    release = described["release"]
    return {
        "distribution": _release(release, "distribution"),
        "package_manager": _release(release, "package-manager"),
    } | described

def set_for_package(distributions: dict[str, dict[str, str]]) -> None:
    """Declare, for one package and everything under it, which distributions its images serve.

    A package that declares no distributions builds its images for every release in
    `catalog//:releases.bzl`. If that file lists one release, the package's images build for it by
    their own names, without aliases. An empty dict declares no distribution. The package's images
    then build by their own names, and their `target_compatible_with` keeps the value that each target
    sets.

    Each entry names one distribution of the package. Its `release` key names a release of the
    catalog, such as `fedora.rawhide`. Its `package_manager` key defaults to the release's package
    manager. The rest is whatever the package needs to know per distribution, and is read back with
    `distribution.for_package()`, which also returns the `distribution` label of the release.

    Every image rule then defaults its `target_compatible_with` to these, so a build that has chosen
    one of them resolves, `//...` skips the rest, and naming a target directly without choosing
    fails. The `distribution` key of the `tine` buckconfig section names the entry that a target's
    own name builds instead. Saying it once per package is the point: repeating it per target is how
    one target ends up forgetting, becoming a target that is itself fine but depends on something
    incompatible, which buck reports as an error rather than a skip.
    """
    write_package_value(
        _DISTRIBUTIONS,
        {name: _describe(name, described) for name, described in distributions.items()},
        overwrite = True,
    )

def for_package() -> dict[str, dict[str, str]]:
    """Return the distributions of this package, keyed by the name it calls each distribution."""
    declared = read_package_value(_DISTRIBUTIONS)
    if declared != None:
        return declared
    return {name: _describe(name, {"release": release}) for name, release in _CATALOG_RELEASES.items()}

def _single() -> bool:
    """Return whether this package builds for the catalog's only release."""
    return read_package_value(_DISTRIBUTIONS) == None and len(_CATALOG_RELEASES) == 1

def _implied() -> str | None:
    """Return the distribution that this package's targets build for when they name none, or None."""
    if _single():
        return _release(_CATALOG_RELEASES.values()[0], "distribution")
    chosen = read_root_config("tine", "distribution")
    declared = for_package()
    if chosen == None or chosen not in declared:
        return None
    return declared[chosen]["distribution"]

def package_manager() -> Select:
    """Return the package manager of the release that a build chooses for this package."""
    declared = for_package()
    if not declared:
        fail("distribution.package_manager(): this package declares no distribution, so name a package manager")
    return by_distribution({described["release"]: described["package_manager"] for described in declared.values()})

def default_package_manager(distro: str | None = None) -> str | Select | None:
    """Return the package manager of an image that names none, or None if the package declares no distribution.

    A target that names `distro` builds for that distribution only, and gets the package manager of
    that release. A `select()` over the package's distributions has no key for a distribution that
    the package does not declare.
    """
    declared = for_package()
    if distro == None:
        return package_manager() if declared else None
    release = distro.rpartition(":")[2].removesuffix(".distribution")
    for described in declared.values():
        if described["release"] == release:
            return described["package_manager"]
    return _release(release, "package-manager")

def _condition(key: str) -> str:
    if key == "DEFAULT":
        return key
    if key in FAMILIES:
        return "tine//distribution:" + key
    return _release(key, "distribution")

# `distribution.select`; a def of that name would shadow the builtin it wraps.
def by_distribution(values: dict[str, typing.Any]) -> Select:
    """A `select()` keyed by family, such as `fedora`, by catalog release, such as `fedora.43`, or by `DEFAULT`.

    Buck picks a release's key over the key of its family. Buck fails to configure a target that
    matches no key.
    """
    return select({_condition(key): value for key, value in values.items()})

def release_target(role: str) -> Select:
    """Return a `select()` of the catalog target `role`, such as `box`, of the release that a build chooses.

    A build that chooses no release, or a release that `catalog//:releases.bzl` doesn't list, gets the
    target of the catalog's default release.
    """
    return by_distribution(
        {release: _release(release, role) for release in _CATALOG_RELEASES.values()} | {"DEFAULT": "catalog//:default." + role},
    )

_UNCHOSEN = "tine//distribution:no-distribution-chosen"

def compatibility():
    """Say a target is buildable only where one of this package's distributions was chosen.

    `target_compatible_with` requires every constraint in its list, so "one of these" is a select
    that asks for nothing under each distribution and for the unsatisfiable value under anything
    else. A package that declared no distributions constrains nothing. Rules tine owns get this
    through their macros; a rule it does not own, such as a prelude one over an image, asks here.

    A package that builds for the catalog's only release, or for the distribution that the
    `distribution` key of the `tine` buckconfig section names, gets no constraint either. A rule
    that tine does not own takes no incoming transition. The select would make a target of that
    rule incompatible wherever it is built by its own name.
    """
    declared = for_package()
    if not declared or _implied() != None:
        return []
    return _any(declared)

def _any(declared: dict[str, dict[str, str]]) -> Select:
    targets = [described["distribution"] for described in declared.values()]
    return select({target: [] for target in targets} | {"DEFAULT": [_UNCHOSEN]})

def _chosen(distro: str) -> list[str] | Select:
    """`target_compatible_with` for a target whose incoming transition chooses `distro`.

    The transition leaves the distribution unset for an architecture that `distro` does not serve,
    and the target is then incompatible.
    """
    declared = for_package()
    if declared:
        # A target built by its own name for the distribution that tine.distribution names is also
        # built for every other distribution of the package through its aliases.
        return _any(declared)
    return select({distro: [], "DEFAULT": [_UNCHOSEN]})

def attributes(distro: str | None = None, visibility: list[str] | None = None) -> dict:
    """Resolve what a package declares about distributions into the attributes rules take.

    Anything a build can name takes these through `distribution.distributed()`, which declares the aliases that
    make the compatibility satisfiable. This is on its own only for a target reached exclusively as
    a dependency, which is configured by whatever depends on it and never named directly.
    """
    result = {"visibility": visibility}
    if distro != None:
        result["incoming_transition"] = distro

    # A package that declares none leaves compatibility alone, which is every package that has not
    # opted into building for a distribution at all.
    compatible_with = compatibility() if distro == None else _chosen(distro)
    if compatible_with:
        result["target_compatible_with"] = compatible_with
    return result

def aliases(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    compatible_with: list[str] | Select = [],
) -> None:
    """Name one target once per distribution its package serves.

    A target that names no distribution of its own is buildable under every one of them, so the
    names that say which are declared beside it rather than listed again somewhere else. A target
    that does name one is already the distribution it is, and gets no aliases. Neither does a target
    of a package that builds for the catalog's only release, because it builds for that release by
    its own name.
    """
    if distro != None or _single():
        return
    declared = for_package()
    chosen = read_root_config("tine", "distribution")
    for suffix in sorted(declared):
        alias(
            name = "{}.{}".format(name, suffix),
            actual = ":" + name,
            distro = declared[suffix]["distribution"],
            visibility = visibility,
            compatible_with = compatible_with,
            # The target's own name already runs the test for the distribution that
            # tine.distribution names. If the alias ran it too, `buck test //...` would run it twice.
            test = suffix != chosen,
        )

def distributed(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
) -> dict:
    """Declare a target's per-distribution aliases and return the attributes it takes.

    These are one decision rather than two. A target given the attributes without the aliases is
    compatible only with a choice nothing can make of it: it disappears from `//...` and fails when
    something finally names it. One that nothing names directly, because it is only ever a
    dependency, takes `distribution.attrs()` on its own instead.

    `target_compatible_with` constrains the target and its aliases alike, on top of the
    distributions. Buck reports an alias that is compatible where its target is not as an error, and
    `//...` then fails instead of skipping both.
    """
    compatible_with = [] if target_compatible_with == None else target_compatible_with
    aliases(name, distro, visibility, compatible_with)
    result = attributes(distro if distro != None else _implied(), visibility)
    result["target_compatible_with"] = result.get("target_compatible_with", []) + compatible_with
    return result

def serves(name: str, architectures: list[str], in_box: bool = False) -> Select:
    """`target_compatible_with` for a target `name` that exists only for `architectures`, such as a release's repository.

    A target that depends on the release's root box, such as the release, its package manager and its
    buildroot, sets `in_box`. The release builds its root box only for `architectures`, so such a target
    is also incompatible on a host whose architecture is not in `architectures`.
    """
    unserved = select_incompatible("{} serves {} only".format(name, ", ".join(architectures)))
    if in_box and architecture.host() not in architectures:
        return select({"DEFAULT": unserved})
    return architecture.select({arch: [] if arch in architectures else unserved for arch in architecture.ARCHITECTURES})

def _distribution_alias_impl(ctx: AnalysisContext) -> list[Provider]:
    test = ctx.attrs.actual.get(ExternalRunnerTestInfo)
    if ctx.attrs.test or test == None:
        return ctx.attrs.actual.providers
    return [provider for provider in ctx.attrs.actual.providers if provider != test]

_distribution_alias = rule(
    impl = _distribution_alias_impl,
    attrs = {
        "actual": attrs.dep(),
        "test": attrs.bool(default = True, doc = "whether `buck test` runs the target's test through the alias"),
    },
    supports_incoming_transition = True,
)

def alias(
    name: str,
    actual: str,
    distro: str,
    visibility: list[str] | None = None,
    compatible_with: list[str] | Select = [],
    test: bool = True,
) -> None:
    """The same target, built for another distribution.

    Everything below it is reconfigured, so one declaration serves every distribution rather than
    being written out once per distribution. The alias is incompatible with an architecture the
    distribution does not serve, and it takes the target's own `compatible_with`. With `test = False`,
    `buck test` does not run the target's test through the alias.
    """
    _distribution_alias(
        name = name,
        actual = actual,
        incoming_transition = distro,
        target_compatible_with = _chosen(distro) + compatible_with,
        visibility = visibility,
        test = test,
    )

distribution = struct(
    FAMILIES = FAMILIES,
    TOOLS_BOX_ATTR = TOOLS_BOX_ATTR,
    alias = alias,
    aliases = aliases,
    attrs = attributes,
    box_host = box_host,
    default_release = default_release,
    default_package_manager = default_package_manager,
    select = by_distribution,
    serves = serves,
    compatibility = compatibility,
    distributed = distributed,
    for_package = for_package,
    new = new,
    package_manager = package_manager,
    release_target = release_target,
    set_for_package = set_for_package,
)
