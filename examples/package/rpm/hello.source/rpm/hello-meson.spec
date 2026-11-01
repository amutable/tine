# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: hello
Version: 1
Release: %autorelease
Summary: Exercise source-tree RPM package builds with meson
License: MPL-2.0
BuildRequires: gcc
BuildRequires: meson

%description
An integration fixture for tine's source-tree RPM package build path with meson.

%build
%meson
%meson_build

%install
%meson_install

%files
%{_bindir}/tine-package-hello
