# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: hello
Version: 1
Release: %autorelease
Summary: Exercise RPM builds from a fetched dist-git repository
License: MPL-2.0
BuildArch: noarch
# The dist-git repository contains Source0, and the BUCK file lists Source1 in srcs.
Source0: local-note
Source1: distgit-extra

%description
An integration fixture for tine's build path from a dist-git repository.

%prep

%build

%install
install -d %{buildroot}%{_datadir}/tine-package
install -pm0644 %{SOURCE0} %{SOURCE1} %{buildroot}%{_datadir}/tine-package/

%files
%{_datadir}/tine-package/local-note
%{_datadir}/tine-package/distgit-extra
