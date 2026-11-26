# Security Reporting

Please do not post exploit details, credentials, private repository URLs, or unredacted customer artifacts in public issues.

Use GitHub private vulnerability reporting in the repository Security tab if enabled, or open a minimal issue requesting a private contact channel without including sensitive details.

## Scope & Guarantees

- `fbk.py` executes local `git` and `lsof` commands only and makes zero outbound network requests (unless you explicitly pass `--fetch` to `audit-remote`, which delegates directly to `git fetch --prune`).
- `fbk.py` never passes user branch names or file paths through `shell=True`; all subprocess invocations use argument arrays (`subprocess.run([...])`).
- Path extraction in `restore --extract-dirty-to` validates every archive member against path traversal (`is_subpath`) and skips symlinks/hardlinks.
