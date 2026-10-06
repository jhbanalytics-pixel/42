# ADR 0004: Package layout ,  src/ flat with setuptools find

**Date:** 2026-04-14
**Author:** Albert Meintjes
**Status:** Active

## Decision

Keep the existing `src/` directory layout. Make it installable via `pyproject.toml` with
`[tool.setuptools.packages.find] where = ["src"]`. Imports stay as
`from ingestion.connectors.base import BaseConnector`.

## Context

The project started with a `src/` layout where `ingestion/`, `scoring/`, `analysis/`, and
`utils/` are top-level packages under `src/`. Renaming or restructuring these now would
break the existing working imports and require touching every file in the codebase at a
point when the connector porting work is just beginning.

## Consequences

`pip install -e .` makes the packages importable without manipulating `sys.path`. CI and
local tests use the same import paths. The `Private :: Do Not Upload` classifier in
`pyproject.toml` prevents accidental PyPI publish.

If a top-level `trends_engine` namespace becomes necessary for packaging alongside other
WPP projects, that rename can happen post-launch as a dedicated refactor with a migration
plan. There is no value in doing it before May 1.
