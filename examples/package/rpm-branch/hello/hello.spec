# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: hello
Version: 1
Release: %autorelease
Summary: Exercise an imported package whose archive is in a lookaside cache
License: MPL-2.0
BuildArch: noarch
# The sources file of the package lists Source0, which the build downloads from the lookaside cache of
# Fedora.
Source0: hello-2.12.3.tar.gz.sig

%description
An integration fixture for tine's build of an imported package with a lookaside archive.

%prep

%build

%install
install -Dpm0644 %{SOURCE0} %{buildroot}%{_datadir}/tine-package/hello-2.12.3.tar.gz.sig

%files
%{_datadir}/tine-package/hello-2.12.3.tar.gz.sig
