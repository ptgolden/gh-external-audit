#!/usr/bin/env bash
set -euo pipefail

ORGANIZATION_NAME="${ORGANIZATION_NAME:-monarch-initiative}"
query="uses org:${ORGANIZATION_NAME} in:file path:.github/workflows"
out="workflow-uses-search-${ORGANIZATION_NAME}.json"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

gh api --paginate --method GET search/code \
  -H 'Accept: application/vnd.github.text-match+json' \
  -f q="$query" \
  -f per_page=100 > "$tmpdir/pages.json"

jq -s --arg query "$query" '
  {
    query: $query,
    total_count: (.[0].total_count // 0),
    incomplete_results: (map(.incomplete_results) | any),
    item_count: (map(.items | length) | add),
    items: (map(.items) | add)
  }
' "$tmpdir/pages.json" > "$out"

printf 'Wrote %s\n' "$out"
