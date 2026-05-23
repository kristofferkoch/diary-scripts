#!/usr/bin/env bash
# Check that the host has the tools this repo's scripts depend on.
#
# Required: anything scripts/ touches directly when working in this repo.
# Optional: mail-sync / tailnet-host bits that only apply on server.
#
# Exit 0 if all required tools are present (optional ones may be missing).
# Exit 1 if any required tool is missing.

set -uo pipefail

if [ -t 1 ]; then
	RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; DIM=$'\e[2m'; RST=$'\e[0m'
else
	RED=''; YEL=''; GRN=''; DIM=''; RST=''
fi

missing_required=0
missing_optional=0

# check <required|optional> <tool> <install hint> [extra_search_paths...]
# extra_search_paths: absolute paths to also probe if PATH lookup fails — for
# tools that are typically installed outside PATH (e.g. ~/go/bin/goimapnotify).
check() {
	local kind="$1" tool="$2" hint="$3"
	shift 3
	local found=""
	if command -v "$tool" >/dev/null 2>&1; then
		found=$(command -v "$tool")
	else
		for p in "$@"; do
			if [ -x "$p" ]; then found="$p"; break; fi
		done
	fi
	if [ -n "$found" ]; then
		printf '  %sOK%s  %-18s %s%s%s\n' "$GRN" "$RST" "$tool" "$DIM" "$found" "$RST"
		return
	fi
	if [ "$kind" = required ]; then
		printf '  %sMISS%s %-18s %s\n' "$RED" "$RST" "$tool" "$hint"
		missing_required=$((missing_required + 1))
	else
		printf '  %sWARN%s %-18s %s\n' "$YEL" "$RST" "$tool" "$hint"
		missing_optional=$((missing_optional + 1))
	fi
}

echo "== required (scripts/ + general repo use) =="
check required notmuch     "sudo dnf install notmuch"
check required pdftotext   "sudo dnf install poppler-utils"
check required uv          "curl -LsSf https://astral.sh/uv/install.sh | sh"
check required git         "sudo dnf install git"
check required shellcheck  "sudo dnf install ShellCheck"

echo
echo "== optional (mail-sync host — currently only server) =="
check optional mbsync         "sudo dnf install isync"
check optional proton-bridge  "build from ~/src/proton-bridge (see memory/2026-05-22.md)" \
	"$HOME/src/proton-bridge/proton-bridge"
check optional goimapnotify   "go install gitlab.com/shackra/goimapnotify/cmd/goimapnotify@latest" \
	"$HOME/go/bin/goimapnotify"

echo
echo "== optional (tailnet reverse-proxy host — currently only server) =="
check optional caddy      "sudo dnf install caddy"
check optional tailscale  "see https://tailscale.com/download/linux/fedora"

# uv handles the Python toolchain itself (pyproject.toml pins >=3.14), so no
# separate python3 check — `uv run` will fetch a matching interpreter if needed.

echo
if [ "$missing_required" -gt 0 ]; then
	printf '%sFAIL%s  %d required tool(s) missing.\n' "$RED" "$RST" "$missing_required"
	exit 1
fi

# Lint every shell script in scripts/. Required-check above guarantees
# the linter binary is on the host.
echo "== shellcheck scripts/*.sh + install_notmuch_hooks =="
repo_root=$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --show-toplevel)
shell_scripts=()
while IFS= read -r f; do shell_scripts+=("$f"); done < <(
	find "$repo_root/scripts" -maxdepth 1 -type f \
		\( -name '*.sh' -o -name 'install_notmuch_hooks' -o -name 'notmuch-post-new' \)
)
if shellcheck "${shell_scripts[@]}"; then
	printf '  %sOK%s  %d script(s) clean\n' "$GRN" "$RST" "${#shell_scripts[@]}"
else
	printf '  %sFAIL%s shellcheck reported issues\n' "$RED" "$RST"
	exit 1
fi

echo
if [ "$missing_optional" -gt 0 ]; then
	printf '%sOK%s    all required tools present (%d optional missing — fine off server).\n' \
		"$GRN" "$RST" "$missing_optional"
else
	printf '%sOK%s    all tools present.\n' "$GRN" "$RST"
fi
