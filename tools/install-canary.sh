#!/bin/bash
# Verify that skills CLI installs a complete, runnable copy for Codex and Claude Code.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-${ROOT}/skills/fireworks-branch-killer}"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/fbk-install-canary.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/home" "$TMP_ROOT/npm-cache" "$TMP_ROOT/state" "$TMP_ROOT/repo"

HOME="$TMP_ROOT/home" npm_config_cache="$TMP_ROOT/npm-cache" \
    npx -y skills@1.5.17 add "$SOURCE" \
    --agent codex claude-code -g -y --copy

for install_root in \
    "$TMP_ROOT/home/.agents/skills/fireworks-branch-killer" \
    "$TMP_ROOT/home/.claude/skills/fireworks-branch-killer"
do
    test -f "$install_root/SKILL.md"
    for directory in agents assets bin docs references scripts tests; do
        test -d "$install_root/$directory"
    done
    cmp "$install_root/SKILL.md" "$ROOT/SKILL.md"
    cmp "$install_root/agents/openai.yaml" "$ROOT/agents/openai.yaml"
    cmp "$install_root/config.example.json" "$ROOT/config.example.json"
    cmp "$install_root/scripts/fbk.py" "$ROOT/scripts/fbk.py"
    cmp "$install_root/scripts/hook_adapter.py" "$ROOT/scripts/hook_adapter.py"
    cmp "$install_root/bin/fbk.js" "$ROOT/bin/fbk.js"
    test -f "$install_root/references/safety-policy.md"
    test -f "$install_root/references/automation-setup.md"
    test -f "$install_root/assets/logo.png"
    test -f "$install_root/assets/diagrams/four-tier-architecture.svg"
    test -x "$install_root/scripts/fbk.py"
    test -x "$install_root/scripts/hook_adapter.py"
    test -x "$install_root/bin/fbk.js"
done

INSTALLED_SKILL="$TMP_ROOT/home/.agents/skills/fireworks-branch-killer"
FBK_STATE_DIR="$TMP_ROOT/state" python3 "$INSTALLED_SKILL/scripts/fbk.py" version --format json > "$TMP_ROOT/version.json"
FBK_STATE_DIR="$TMP_ROOT/state" python3 "$INSTALLED_SKILL/scripts/fbk.py" doctor --workspace "$TMP_ROOT/home" --format json > "$TMP_ROOT/doctor.json"

git init -b main "$TMP_ROOT/repo" >/dev/null
git -C "$TMP_ROOT/repo" config user.email "canary@example.com"
git -C "$TMP_ROOT/repo" config user.name "Canary"
echo "hello" > "$TMP_ROOT/repo/README.md"
git -C "$TMP_ROOT/repo" add README.md
git -C "$TMP_ROOT/repo" commit -m "init" >/dev/null

FBK_STATE_DIR="$TMP_ROOT/state" node "$INSTALLED_SKILL/bin/fbk.js" scan --repo "$TMP_ROOT/repo" --format json > "$TMP_ROOT/scan.json"
test -s "$TMP_ROOT/version.json"
test -s "$TMP_ROOT/doctor.json"
test -s "$TMP_ROOT/scan.json"

echo "install canary passed: $SOURCE"
