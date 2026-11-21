# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: source-tree
Version: 1
Release: %autorelease
Summary: Copy the source tree into the package
License: MPL-2.0
BuildArch: noarch

%description
An integration fixture for project.source_tree().

%install
mkdir -p %{buildroot}%{_datadir}/source-tree
cp -a . %{buildroot}%{_datadir}/source-tree/

%files
%{_datadir}/source-tree
