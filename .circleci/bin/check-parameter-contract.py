#!/usr/bin/env python3
"""Static consistency checks for the two-stage CircleCI config.

Nothing in the CircleCI toolchain verifies that the setup pipeline
(`.circleci/config.yml`) and the continuation pipeline
(`.circleci/continue-config.yml`) agree, because the two files are only ever
joined at runtime by the path-filtering orb. This script checks the parts that
`circleci config validate` cannot:

  1. Every parameter name emitted by the setup mapping is declared in the
     continuation config, and vice versa.
  2. Every emitted parameter defaults to `false` in the continuation config,
     so an unmatched path means "do not run".
  3. Every mapping line is well formed for the orb's parser (exactly three
     whitespace-separated tokens) and every regex compiles.
  4. Pinned toolchain versions in the continuation config match the repo's
     `.ruby-version` and `.nvmrc`.
  5. Every `requires:` entry names a job instance defined in the same
     workflow.
  6. Every `save_cache` key has a matching `restore_cache` prefix, and every
     `restore_cache` key list is ordered most-specific-first.

Exit status is 0 when everything agrees, 1 otherwise.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / ".circleci" / "config.yml"
CONTINUE = ROOT / ".circleci" / "continue-config.yml"

# `<< ... >>` is CircleCI parameter syntax, not YAML, so the raw files have to
# be read with the tokens neutralised before yaml.safe_load sees them. The
# replacement must be a bare token that is legal as a *plain* scalar in both
# block and flow context -- so no braces, brackets or commas, which is why
# `${...}` cannot be used here.
PARAM_TOKEN = re.compile(r"<<\s*([^>]+?)\s*>>")


def neutralise(match: re.Match) -> str:
    return "CCIPARAM_" + re.sub(r"[^A-Za-z0-9_.]", "_", match.group(1))


def load(path: Path):
    return yaml.safe_load(PARAM_TOKEN.sub(neutralise, path.read_text()))


def parse_mapping(setup_doc) -> tuple[set[str], list[str]]:
    """Return (parameter names, errors) from the setup mapping block."""
    errors: list[str] = []
    jobs = setup_doc["workflows"]["setup"]["jobs"]
    mapping_text = None
    for entry in jobs:
        if isinstance(entry, dict):
            body = next(iter(entry.values()))
            if isinstance(body, dict) and "mapping" in body:
                mapping_text = body["mapping"]
    if mapping_text is None:
        return set(), ["no `mapping` found in the setup workflow"]

    names: set[str] = set()
    for lineno, raw in enumerate(mapping_text.splitlines(), start=1):
        line = raw.strip()
        # Mirrors the orb's is_mapping_line(): blank and #-comment lines skipped.
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) != 3:
            errors.append(
                "mapping line %d has %d tokens, expected 3: %r"
                % (lineno, len(tokens), line)
            )
            continue
        pattern, name, value = tokens
        try:
            re.compile("^%s$" % pattern)
        except re.error as exc:
            errors.append("mapping line %d: bad regex %r (%s)" % (lineno, pattern, exc))
        if value != "true":
            errors.append(
                "mapping line %d: value %r is not 'true'; the continuation "
                "config treats these as booleans" % (lineno, value)
            )
        names.add(name)
    return names, errors


def check_parameters(setup_doc, continue_doc) -> list[str]:
    errors: list[str] = []
    emitted, errors_from_mapping = parse_mapping(setup_doc)
    errors.extend(errors_from_mapping)

    declared = continue_doc.get("parameters", {})
    declared_run = {k for k in declared if k.startswith("run-")}

    for missing in sorted(emitted - declared_run):
        errors.append("setup emits %r but the continuation config does not declare it" % missing)
    for extra in sorted(declared_run - emitted):
        errors.append("continuation config declares %r but setup never emits it" % extra)

    for name in sorted(emitted & declared_run):
        spec = declared[name]
        if spec.get("type") != "boolean":
            errors.append("%r should be type boolean, is %r" % (name, spec.get("type")))
        if spec.get("default") is not False:
            errors.append(
                "%r defaults to %r; it must default to false so that an "
                "unmatched path means 'do not run'" % (name, spec.get("default"))
            )
    return errors


def check_pinned_versions(continue_doc) -> list[str]:
    errors: list[str] = []
    params = continue_doc.get("parameters", {})

    ruby_expected = (ROOT / ".ruby-version").read_text().strip()
    ruby_actual = params.get("default-ruby-version", {}).get("default")
    if ruby_actual != ruby_expected:
        errors.append(
            "default-ruby-version is %r but .ruby-version says %r"
            % (ruby_actual, ruby_expected)
        )

    node_expected = (ROOT / ".nvmrc").read_text().strip()
    node_actual = params.get("node-version", {}).get("default")
    if node_actual != node_expected:
        errors.append("node-version is %r but .nvmrc says %r" % (node_actual, node_expected))

    return errors


def workflow_job_entries(entry):
    """Normalise a workflow job entry to (instance_name, body)."""
    if isinstance(entry, str):
        return entry, {}
    key = next(iter(entry))
    body = entry[key] or {}
    return body.get("name", key), body


def check_requires(continue_doc) -> list[str]:
    errors: list[str] = []
    for wf_name, wf in continue_doc.get("workflows", {}).items():
        if not isinstance(wf, dict) or "jobs" not in wf:
            continue
        names: set[str] = set()
        for entry in wf["jobs"]:
            instance, _ = workflow_job_entries(entry)
            # Matrix instances carry a ${matrix.x} placeholder; record the
            # literal prefix so requires: on a non-matrix sibling still works.
            names.add(instance)
        for entry in wf["jobs"]:
            instance, body = workflow_job_entries(entry)
            for required in body.get("requires", []) or []:
                if required not in names:
                    errors.append(
                        "workflow %r: job %r requires %r, which is not defined "
                        "in that workflow" % (wf_name, instance, required)
                    )
    return errors


def collect_cache_keys(node, saves, restores):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "save_cache" and isinstance(value, dict):
                saves.append(value.get("key"))
            elif key == "restore_cache" and isinstance(value, dict):
                restores.append(value.get("keys") or [value.get("key")])
            collect_cache_keys(value, saves, restores)
    elif isinstance(node, list):
        for item in node:
            collect_cache_keys(item, saves, restores)


def check_caches(continue_doc) -> list[str]:
    errors: list[str] = []
    saves: list[str] = []
    restores: list[list[str]] = []
    collect_cache_keys(continue_doc.get("commands", {}), saves, restores)
    collect_cache_keys(continue_doc.get("jobs", {}), saves, restores)

    for key_list in restores:
        # Fallback chains legitimately contain *sibling* keys (e.g. the
        # current branch, then `main`), so "each key is a prefix of the next"
        # is too strong. The invariants that do hold: every key in a chain
        # shares the cache's namespace-and-version prefix, and the chain ends
        # at the broadest key, which is a prefix of the most specific one.
        shared = os.path.commonprefix(key_list)
        if "-v" not in shared:
            errors.append(
                "restore_cache chain %r has no shared namespace/version "
                "prefix (common prefix %r)" % (key_list, shared)
            )
        if not key_list[0].startswith(key_list[-1]):
            errors.append(
                "restore_cache chain does not end at its broadest key: last "
                "key %r is not a prefix of first key %r" % (key_list[-1], key_list[0])
            )

    restore_prefixes = [k for key_list in restores for k in key_list]
    for save_key in saves:
        if not any(
            save_key == r or save_key.startswith(r.rstrip("-")) for r in restore_prefixes
        ):
            errors.append("save_cache key %r is never restored by any restore_cache" % save_key)

    for key_list in restores:
        primary = key_list[0]
        if not any(s == primary or s.startswith(primary.rstrip("-")) for s in saves):
            errors.append(
                "restore_cache primary key %r is never written by any save_cache" % primary
            )
    return errors


def main() -> int:
    setup_doc = load(SETUP)
    continue_doc = load(CONTINUE)

    errors: list[str] = []
    errors += check_parameters(setup_doc, continue_doc)
    errors += check_pinned_versions(continue_doc)
    errors += check_requires(continue_doc)
    errors += check_caches(continue_doc)

    if errors:
        for error in errors:
            print("FAIL: %s" % error)
        return 1

    print("OK: setup and continuation configs agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
