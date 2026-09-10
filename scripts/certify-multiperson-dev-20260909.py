#!/usr/bin/env python3
"""Retired unsafe dev certification entry point.

This historical script rendered fully resolved Docker Compose configuration to the
terminal, which can expose environment secrets. It is intentionally non-executable
as a certification path. Use the immutable V3 integrity hardening/certification
launcher introduced on 2026-09-10 instead.
"""

raise SystemExit(
    "RETIRED: this certification path could expose resolved environment secrets. "
    "Use scripts/run-v3-integrity-hardening-dev-20260910.sh instead."
)
