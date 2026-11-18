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
reports each pipeline as `installed`, `updated`, `unchanged`, or `failed`, with how
many of its commands were enabled, and exits nonzero if any pipeline fails. Existing
commands are updated in place (keeping their site and project enablement), and
commands absent from the catalogue are not removed. The XNAT user must be an
administrator or a Container Service manager.

### Selecting and enabling pipelines

A YAML config file passed to `--config` (or `PIPELINE_RECONCILER_CONFIG`) selects
which catalogue pipelines are reconciled and whether their commands are enabled:

```yaml
include:              # optional; leave out to take every pipeline
  - mri.human.neuro.*
exclude:              # optional; wins over include
  - mri.human.neuro.bidsapp.*
enablement: approval  # or "auto"; default is approval
```

Patterns are case-sensitive wildcards matched against the whole
catalogue pipeline ID, where `*` also matches `.`. Excluded pipelines are left
untouched even if already installed: their commands are not downloaded, updated or
enabled. Invalid config fails before XNAT is contacted, and a pattern matching no
pipeline logs a warning. Without a config, every pipeline is reconciled in `approval`
mode.

- `approval`: new commands are installed disabled until an administrator enables them.
- `auto`: every selected command is enabled for the site on each run, including any
  an administrator has disabled. To block a pipeline, exclude it, then disable it in
  XNAT.

Either way, project owners still enable commands for their own projects.

## Pipeline assessor outputs (XNAT 1.10+)

Opt in per command, then rebuild the image:

```yaml
commands:
  concatenate:
    task: frametree.testing.tasks:Concatenate
    operates_on: medimage/session
    assessor_outputs: true
```

`deploy-pipelines` and `install-command` register the type and form; this requires
a site administrator. Pipeline runs use the launching user's project permissions.
Installing only through image-label discovery does not register them.

| Frequency | Results are attached to |
| --- | --- |
| `medimage/session` | Imaging session |
| `medimage/subject` | Subject |
| `medimage/dataset` | Project asset |

Each run processes one session, one subject, or a whole project, respectively.
Subject inputs are retrieved through the API because Container Service cannot
mount a subject's archive.

List scalar outputs explicitly in `sinks` (supported: string, integer, float and
boolean). Outputs are staged until the run succeeds. Files are stored in
output-named resources; scalar results and run details are in a Custom Form, with
full provenance in `PROVENANCE/provenance.json`. Form fields are available in
Advanced Search. Reruns reuse the same assessor and reconcile its output files.

Types and run labels use readable pipeline names. Types are stable; schema changes
create a new form revision, preserving previous forms and values. Existing types,
forms and labels are not renamed. Default XNAT layouts are unchanged. Project
results are searchable project assets, but XNAT 1.10 has no standard detail page
for browsing their files.

Commands without opt-in are unchanged. On XNAT older than 1.10, assessor outputs
fall back to the existing storage behavior. `internal_upload` is not supported.
Reading assessors as FrameTree derivatives is separate work in `frametree-xnat`.

For Container Service REST launches, `root` must be the wrapper input name:
`SESSION`, `SUBJECT` or `PROJECT`—not an XNAT datatype.

## License

This work is licensed under a [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](http://creativecommons.org/licenses/by-nc-sa/4.0/)

![Creative Commons License: Attribution-NonCommercial-ShareAlike 4.0 International](https://i.creativecommons.org/l/by-nc-sa/4.0/88x31.png)
  [Creative Commons License: Attribution-NonCommercial-ShareAlike 4.0 International](http://creativecommons.org/licenses/by-nc-sa/4.0/)
