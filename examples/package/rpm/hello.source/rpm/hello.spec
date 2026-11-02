# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: hello
Version: 1
Release: %autorelease
Summary: Exercise dev-mode source-tree RPM package builds
License: MPL-2.0
BuildRequires: gcc
# The base buildroot doesn't contain jq, and the BUCK file doesn't list it.
%if "%(echo jq > generated-buildrequires && cat generated-buildrequires)" == "jq"
BuildRequires: jq
%endif
Source1: packaging-marker

%description
An integration fixture for tine's source-tree RPM package build path.

%build
mkdir -p %{_vpath_builddir}
lto=disabled
%if 0%{?_lto_cflags:1}
lto=enabled
%endif
annobin=disabled
%if 0%{?_annotated_build:1}
annobin=enabled
%endif
printf '%s\n' "$lto" > %{_vpath_builddir}/lto
printf '%s\n' "$annobin" > %{_vpath_builddir}/annobin
%{__cc} %{build_cflags} %{build_ldflags} -o %{_vpath_builddir}/hello hello.c
jq -n '"checkout"' > %{_vpath_builddir}/jq

%install
install -Dpm0755 %{_vpath_builddir}/hello %{buildroot}%{_bindir}/tine-package-hello
install -Dpm0644 %{SOURCE1} %{buildroot}%{_datadir}/tine-package/spec-from-checkout
install -Dpm0644 %{_vpath_builddir}/jq %{buildroot}%{_datadir}/tine-package/built-with-jq
install -d %{buildroot}%{_datadir}/tine-package
lto="$(cat %{_vpath_builddir}/lto)"
annobin="$(cat %{_vpath_builddir}/annobin)"
touch "%{buildroot}%{_datadir}/tine-package/profile-lto-$lto"
touch "%{buildroot}%{_datadir}/tine-package/profile-annobin-$annobin"

%files
%{_bindir}/tine-package-hello
%{_datadir}/tine-package/*
