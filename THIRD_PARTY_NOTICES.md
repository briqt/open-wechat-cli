# Third-party notices

## Runtime dependencies

Dependencies are installed by Python packaging tools and retain their own licenses. This repository does not vendor their implementations.

| Dependency | Purpose | Upstream / license |
|---|---|---|
| PyCryptodome | AES page decoding | https://www.pycryptodome.org/src/license — BSD/public-domain components; follow the installed distribution's notices |
| tzdata | IANA time-zone data on systems without a system database | https://github.com/python/tzdata — Apache-2.0 package; underlying time-zone data has its own public-domain notices |

Development/build tools (`pytest`, `setuptools`, `build`) are not part of the product implementation and keep their upstream licenses.

## Format references

The wxSQLite3-style page layout and page-number key/IV derivation were cross-checked against public descriptions and implementations. References are not bundled runtime dependencies or an additional license grant over upstream code:

- https://github.com/huangserva/wechat-radar
- https://github.com/NOBB2333/wechat-analysis-action/tree/main/core-wecom
- https://github.com/utelle/wxsqlite3

No client binary, database, key, process dump or real message export is distributed in this repository. Before adding vendored code, preserve its source/version and original license notices and update this file.
