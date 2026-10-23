#!/usr/bin/env bash
# Disable every upstream workflow on a fork except the ones the fork relies on.
#
# GitHub registers a fork's workflows only after its owner enables Actions in
# the repository's Actions tab. Do that once, then run:
#
#   scripts/fork_trim_workflows.sh <owner>/<repo>
#
# Workflows are disabled, not deleted, so rebases on upstream stay clean.
set -euo pipefail

repo="${1:?usage: $0 <owner>/<repo>}"

keep=(
  flywheel-ci.yml
  ci.yml
  lint.yml
  web-tests.yml
  server-compat.yml
  security-scan.yml
  docker-build.yml
)

registered="$(gh api "repos/${repo}/actions/workflows" --paginate \
  --jq '.workflows[] | [.id, (.path | split("/") | last), .state] | @tsv')"

if [ -z "$registered" ]; then
  echo "No workflows are registered on ${repo}." >&2
  echo "Enable Actions in https://github.com/${repo}/actions first, then rerun." >&2
  exit 1
fi

disabled=0
kept=0
while IFS=$'\t' read -r workflow_id file state; do
  if printf '%s\n' "${keep[@]}" | grep -qx "$file"; then
    if [ "$state" != "active" ]; then
      gh workflow enable "$workflow_id" -R "$repo"
    fi
    kept=$((kept + 1))
  elif [ "$state" = "active" ]; then
    gh workflow disable "$workflow_id" -R "$repo"
    disabled=$((disabled + 1))
  fi
done <<< "$registered"

echo "Kept ${kept} workflows active, disabled ${disabled} on ${repo}."
