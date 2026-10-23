#!/usr/bin/env bash
# Stand up the flywheel distribution on Railway with one command.
#
# Creates a project with a Postgres database and an `omnigent` service built
# from this repository's `dist` branch (railway.toml builds the source
# Dockerfile, web UI included), wires DATABASE_URL, attaches a volume for
# artifacts and server state, generates an admin password and a GitHub webhook
# secret, and gives the service a public *.up.railway.app domain.
#
# Requirements: the Railway CLI, logged in (`railway login`), and Railway's
# GitHub app allowed to read the repository.
#
#   deploy/railway/deploy.sh [project-name]
#
# Environment overrides: OMNIGENT_REPO (owner/name), OMNIGENT_BRANCH.
set -euo pipefail

project_name="${1:-omnigent-flywheel}"
repo="${OMNIGENT_REPO:-juanpasaflipz/omnigent}"
branch="${OMNIGENT_BRANCH:-dist}"
service="omnigent"

command -v railway >/dev/null || { echo "Install the Railway CLI first: https://docs.railway.com/cli" >&2; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "Run 'railway login' first." >&2; exit 1; }

random_secret() { openssl rand -hex "$1"; }
admin_password="$(random_secret 16)"
webhook_secret="$(random_secret 32)"

echo "==> Creating project ${project_name}"
railway init --name "$project_name" >/dev/null

echo "==> Adding Postgres"
railway add --database postgres >/dev/null

echo "==> Adding the ${service} service from ${repo}@${branch}"
# The ${{Postgres.DATABASE_URL}} reference is resolved by Railway, not the shell.
# shellcheck disable=SC2016
railway add --service "$service" --repo "$repo" --branch "$branch" \
  --variables 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
  --variables "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD=${admin_password}" \
  --variables "OMNIGENT_GITHUB_WEBHOOK_SECRET=${webhook_secret}" \
  --variables "OMNIGENT_AUTH_PROVIDER=accounts" \
  --variables "OMNIGENT_ACCOUNTS_AUTO_OPEN=0" \
  --variables "OMNIGENT_DATA_DIR=/data/omnigent" \
  --variables "OMNIGENT_ADMIN_CREDENTIALS_PATH=/data/admin-credentials" \
  --variables "ARTIFACT_DIR=/data/artifacts" >/dev/null

railway service link "$service" >/dev/null

echo "==> Attaching a volume at /data"
railway volume add --mount-path /data >/dev/null

echo "==> Generating a public domain"
railway domain --service "$service"

cat <<EOF

Done. The first build compiles the web UI and the server, so it takes several
minutes. Follow it with:  railway logs --build

Sign in as user "admin" with this password (shown once; it is also stored in
the service's Variables as OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD):

  ${admin_password}

GitHub webhook secret for subscriptions (Variables: OMNIGENT_GITHUB_WEBHOOK_SECRET):

  ${webhook_secret}
EOF
