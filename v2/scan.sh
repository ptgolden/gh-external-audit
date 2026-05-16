#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: scan.sh ORG

Print external GitHub Actions and reusable workflow uses for every repository
in ORG, one dependency per line:

  owner/repo<TAB>workflow-path<TAB>uses-target<TAB>ref

Local uses such as ./action and same-org uses are omitted.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ORG="${1:?usage: $0 ORG}"
REPO_LIMIT="${REPO_LIMIT:-1000}"

for dep in gh curl yq sort; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    printf 'missing required command: %s\n' "$dep" >&2
    exit 1
  fi
done

gh repo list "$ORG" --limit "$REPO_LIMIT" --json nameWithOwner -q '.[].nameWithOwner' |
while read -r repo; do
  workflow_urls="$(
    gh api "repos/$repo/contents/.github/workflows" \
    --jq '.[] | select(.type == "file") | select(.name | test("\\.ya?ml$")) | [.path, .download_url] | @tsv' \
    2>/dev/null || true
  )"

  while IFS=$'\t' read -r workflow_path url; do
    [[ -n "$url" ]] || continue

    curl -fsSL "$url" |
      yq -r '.. | select(tag == "!!map" and has("uses")) | .uses | select(tag == "!!str")' - |
      awk -v repo="$repo" -v workflow_path="$workflow_path" -v org="$ORG" '
        {
          uses=$0
          ref=""

          if (uses ~ /@/) {
            ref=uses
            sub(/^.*@/, "", ref)
          }

          # skip local actions/workflows
          if (uses ~ /^\.\/|^\.\.\//) next

          # split owner/repo/etc
          split(uses, parts, "/")
          owner=parts[1]

          # skip same-org actions/reusable workflows
          if (tolower(owner) == tolower(org)) next

          print repo "\t" workflow_path "\t" uses "\t" ref
        }
      '
  done <<< "$workflow_urls"
done | sort -u
