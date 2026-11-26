# Contributing

`fireworks-branch-killer` welcomes bug fixes, harness adapters, safety-policy tests, and documentation improvements.

## Local Checks

Requirements: Python 3.9+ and `git` (Node.js 20+ optional for `npm pack` distribution checks).

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tools/*.py
python3 tools/check_project_consistency.py
python3 tools/distribution.py --check
```

## Safety Rules for Contributors

1. Every behavioral or safety-tier change MUST ship with an isolated temporary-directory test in `tests/test_fbk.py`.
2. Read-only commands (`scan`, `plan`, `doctor`, `audit-remote`, `version`) must always pass `--no-optional-locks` and never write objects or refs into target repositories.
3. Any destructive path (`clean`, `close`, `slim`, `prune-remotes`, `repair-alternates`) must remain dry-run by default and require `--apply`.
4. Run `python3 tools/distribution.py --sync` after modifying any packaged file so the nested `skills/fireworks-branch-killer/` mirror stays byte-identical.
5. Never commit personal paths, private repository names, tokens, or local `.cache/` state.

## Reporting Bugs

Please include:
- `python3 scripts/fbk.py version --format json`
- `python3 scripts/fbk.py doctor --format json`
- A minimal reproduction in a fresh `/tmp` test repository (redacting any private paths or branch names).

Security issues should follow [SECURITY.md](SECURITY.md).
