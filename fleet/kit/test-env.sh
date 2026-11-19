#!/usr/bin/env bash
# Print the environment that backend tests need on a fleet machine, as export lines.
# The two auth constants are read at run time from the CI workflow, so this kit never carries their values.
# Usage, from the repository root:  eval "$(bash fleet/kit/test-env.sh)"
# The database and Redis point at the isolated test stack of apps/backend-rag/docker-compose.test.yml
# (Postgres on 5433, Redis on 6380), never at a development or production database.
set -euo pipefail
wf=".github/workflows/tests.yml"
if [ ! -f "$wf" ]; then
  echo "run this from the repository root ($wf not found)" >&2
  exit 2
fi
for name in JWT_SECRET_KEY API_KEYS; do
  value=$(grep -m1 -E "^[[:space:]]+${name}:[[:space:]]" "$wf" | sed -E "s/^[[:space:]]+${name}:[[:space:]]*//")
  if [ -z "$value" ]; then
    echo "could not read ${name} from $wf" >&2
    exit 3
  fi
  printf 'export %s=%q\n' "$name" "$value"
done
db_user=test
db_name=test
db_port=5433
printf 'export DATABASE_URL=%q\n' "postgresql://${db_user}:${db_user}@localhost:${db_port}/${db_name}"
echo 'export TEST_DATABASE_URL="$DATABASE_URL" INTAKE_TEST_DSN="$DATABASE_URL"'
echo 'export REDIS_URL=redis://localhost:6380'
echo 'export ENVIRONMENT=test LANGCHAIN_TRACING_V2=false'
# Everything else that could reach a real service is pointed at a dead local port or removed.
# INTERNAL_EMAIL_API_URL defaults to the production app when unset, so it must be set; port 9 refuses at once.
echo 'export INTERNAL_EMAIL_API_URL=http://127.0.0.1:9 QDRANT_URL=http://127.0.0.1:9'
echo 'export MIGRATION_DATABASE_URL="$DATABASE_URL" VISA_SHADOW_TEST_DATABASE_URL="$DATABASE_URL" VISA_ORACLE_SMOKE_DATABASE_URL="$DATABASE_URL"'
echo 'unset SENTRY_DSN INTAKE_DATABASE_URL LOCAL_DATABASE_URL NUZ_UNIVERSAL_PG_DSN NUZ_ADOPTION_PG_DSN INTEL_EVIDENCE_BRIDGE_DSN VISA_ENGINE_PACK_WRITER_DATABASE_URL VISA_ENGINE_ACTIVATION_DATABASE_URL VISA_ENGINE_RETENTION_DATABASE_URL OLLAMA_URL OLLAMA_BASE_URL'
