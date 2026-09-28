# Pydra2App - XNAT
[![tests](https://github.com/arcanaframework/pydra2app-xnat/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/ArcanaFramework/pydra2app-xnat/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/arcanaframework/pydra2app-xnat/branch/main/graph/badge.svg?token=UIS0OGPST7)](https://codecov.io/gh/arcanaframework/pydra2app-xnat)
[![Python versions](https://img.shields.io/pypi/pyversions/pydra2app-xnat.svg)](https://pypi.python.org/pypi/pydra2app-xnat/)
[![Latest Version](https://img.shields.io/pypi/v/pydra2app-xnat.svg)](https://pypi.python.org/pypi/pydra2app-xnat/)
[![docs](https://img.shields.io/badge/docs-latest-brightgreen.svg?style=flat)](https://arcanaframework.github.io/pydra2app)

An extension for the [Pydra2App](http://arcanaframework.github.io/pydra2app) framework that support for building [XNAT](https://xnat.org) "apps" (container service docker images) from Pydra tasks.

## Quick Installation

This extension can be installed for Python 3 using *pip*:

```
$ pip3 install pydra2app-xnat
```

This will also install the core Pydra2App package and any required dependencies.

## Reconcile XNAT pipelines

`deploy-pipelines` performs one reconciliation against a release catalogue and exits:

```bash
PIPELINE_CATALOGUE_URL=https://example.org/pipeline-release.json \
XNAT_HOST=https://xnat.example.org \
XNAT_USER=username \
XNAT_PASS=password \
pydra2app ext xnat deploy-pipelines
```

The catalogue may instead be passed as a local file or URL argument. The command
reports each pipeline as `installed`, `updated`, `unchanged`, or `failed`, and exits
nonzero if any pipeline fails. New commands are installed disabled, existing commands
are updated in place (keeping their site and project enablement), and commands absent
from the catalogue are not removed. The XNAT user must be an administrator or a
Container Service manager.

## License

This work is licensed under a [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](http://creativecommons.org/licenses/by-nc-sa/4.0/)

![Creative Commons License: Attribution-NonCommercial-ShareAlike 4.0 International](https://i.creativecommons.org/l/by-nc-sa/4.0/88x31.png)
  [Creative Commons License: Attribution-NonCommercial-ShareAlike 4.0 International](http://creativecommons.org/licenses/by-nc-sa/4.0/)
