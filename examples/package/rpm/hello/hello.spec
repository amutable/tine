# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

Name: hello
Version: %{?version_override}%{!?version_override:1}
Release: %autorelease
Summary: Exercise regular RPM package builds
License: MPL-2.0
BuildArch: noarch
# The base buildroot doesn't contain jq.
%if "%{getenv:HOME}" == "/build" && "%{getenv:SOURCE_DATE_EPOCH}" == "1754006400"
BuildRequires: jq
%endif

%description
An integration fixture for tine's regular RPM package build path.

%prep

%build
jq -n '"archive"' > jq

%install
install -d %{buildroot}%{_datadir}/tine-package
touch %{buildroot}%{_datadir}/tine-package/archive
install -pm0644 jq %{buildroot}%{_datadir}/tine-package/built-with-jq

%files
%{_datadir}/tine-package/archive
%{_datadir}/tine-package/built-with-jq
