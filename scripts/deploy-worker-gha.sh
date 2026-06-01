#!/usr/bin/env bash
# Deploy the Cloudflare Worker via GitHub Actions (no local pywrangler / python storm).
# Requires: gh CLI logged in, changes pushed to the branch the workflow uses (main).
set -euo pipefail
cd "$(dirname "$0")/.."

branch="${1:-main}"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "WARNING: You have uncommitted changes; GHA will deploy the last push on ${branch}, not your working tree." >&2
  echo "  Commit and push first: git push origin ${branch}" >&2
fi

ahead="$(git rev-list --count "origin/${branch}..HEAD" 2>/dev/null || echo 0)"
if [[ "${ahead}" != "0" ]]; then
  echo "WARNING: ${ahead} commit(s) on HEAD are not on origin/${branch}. Push before deploy." >&2
fi

echo "Triggering Deploy Cloudflare Worker workflow (branch ${branch})..."
gh workflow run "Deploy Cloudflare Worker" --ref "${branch}"
echo "Waiting for run to start..."
sleep 5
run_id="$(gh run list --workflow="Deploy Cloudflare Worker" --limit 1 --json databaseId -q '.[0].databaseId')"
echo "Run: https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/actions/runs/${run_id}"
gh run watch "${run_id}" --exit-status
