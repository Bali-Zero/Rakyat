# Toolchain and local checks for a fleet machine

Measured on 2026-11-19 from a fresh clone at `68bdda3` (Linux x86_64, 4 vCPU, 15 GB RAM):
backend install 81 s (virtualenv 6.7 GB), frontend install 24 s (`node_modules` 1.1 GB),
backend collection 42,335 tests in 79 s. CI uses Python 3.11, Node 24 and Postgres 15 on `ubuntu-latest`.
Every location below is git-ignored or outside the tree, so the tracked tree stays clean.
`<kit>` is the kit directory (`fleet/kit` in the repository, or the private copy under `~/.rakyat-fleet/fleet/kit`).

## 0. Launching a fleet session

```bash
git fetch origin main
git worktree add ~/rakyat-fleet-<machine> origin/main          # detached and clean; one per machine
cd ~/rakyat-fleet-<machine>
git remote get-url origin                                       # must name Bali-Zero/Rakyat
bash <kit>/kit_id.sh                                            # must print the KIT_ID of the owner's prompt
mkdir -p ~/.rakyat-fleet/out
claude --add-dir ~/.rakyat-fleet --permission-mode acceptEdits  # or the owner's usual mode; start INSIDE the worktree
```

- The private output directory is `~/.rakyat-fleet/out/<sector>`: outside the tree, added to the session with
  `--add-dir` so that writes do not stop on a permission prompt. A canary passes only when `status.json` and one map
  file appear there with no prompt.
- With `acceptEdits`, shell commands still ask. Allow the fleet's commands for this worktree only, in
  `.claude/settings.local.json` (git-ignored, never committed): `Bash(git:*)`, `Bash(python3:*)`, `Bash(uv:*)`,
  `Bash(npm:*)`, `Bash(npx:*)`, `Bash(node:*)`, `Bash(bash <kit>/*)`, `Bash(<kit>/t:*)`.
- Headless children (`claude -p`, Codex, Kimi, Gemini) get a sterile configuration directory, never the interactive
  profile with its hooks.

## 1. Worktree

Work in your own clean worktree, never in a checkout another session is using (section 0). The workflow stops if the
tree is dirty or if HEAD carries commits that are not on `origin/main`.

## 2. Backend (Python 3.11, uv, lock-pinned: the recipe of .github/workflows/tests.yml)

```bash
cd apps/backend-rag
uv venv --python 3.11 .venv                                   # **/.venv is git-ignored
VIRTUAL_ENV=$PWD/.venv uv pip install -r requirements.lock.txt -r requirements-test.txt
VIRTUAL_ENV=$PWD/.venv uv pip install -e ../../packages/cell-core
# proof: prints "24 passed"
<kit>/t 'cd apps/backend-rag && PYTHONPATH=.:../crm-cell .venv/bin/python -m pytest backend/tests/services/rag/test_confidence.py -p no:cacheprovider'
```

- `requirements-test.txt` is needed: without it two modules fail to collect (`fakeredis`).
- Every test, migrate or gate command runs through `<kit>/t`, from the worktree root: `<kit>/t '<command>'`. It
  exports the test environment, points internal e-mail and Qdrant at a dead local port, removes the other service
  variables, and refuses to run (exit 3) if any database or Redis variable points outside the isolated stack.
- For many files add `-n auto --dist loadfile`; on a 16-24 GB machine use `-n 2` to `-n 4`.
- Never add `-q`: `pytest.ini` already sets it and `scripts/pytest_guards/pytest_verbosity_guard.py` refuses a second one.

## 3. The isolated test stack: Postgres on 5433, Redis on 6380

Never point a test run at a development or production database. CI itself uses a database named `nuzantara_test` on
5432; the root conftest clones one database per xdist worker, so the test role needs CREATE DATABASE. Pick the recipe
for your machine, then prepare the schema:

```bash
<kit>/t 'cd apps/backend-rag && PYTHONPATH=.:../crm-cell .venv/bin/python scripts/ci_bootstrap_schema.py && PYTHONPATH=.:../crm-cell .venv/bin/python -m backend.db.migrate apply-all'
```

**With Docker** (any machine):

```bash
docker compose -f apps/backend-rag/docker-compose.test.yml up -d postgres-test redis-test   # Postgres 15 on 5433, Redis 7 on 6380
```

**macOS without Docker** (Homebrew; a separate cluster, never the one on 5432):

```bash
brew install postgresql@15 redis
D=~/.rakyat-fleet/pg15; /opt/homebrew/opt/postgresql@15/bin/initdb -D $D -U postgres -A trust
/opt/homebrew/opt/postgresql@15/bin/pg_ctl -D $D -o '-p 5433 -k /tmp' -l $D/pg.log start
/opt/homebrew/opt/postgresql@15/bin/psql -h /tmp -p 5433 -U postgres -c 'CREATE ROLE test LOGIN SUPERUSER' -c 'CREATE DATABASE test OWNER test'
redis-server --port 6380 --daemonize yes
```

**Windows with WSL2** (Ubuntu; Postgres 15 like CI):

```bash
sudo apt-get install -y postgresql-common && sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
sudo apt-get install -y postgresql-15 redis-server
sudo pg_createcluster 15 fleet --port 5433 --start -- --auth=trust
sudo -u postgres psql -p 5433 -c 'CREATE ROLE test LOGIN SUPERUSER' -c 'CREATE DATABASE test OWNER test'
redis-server --port 6380 --daemonize yes
```

**Cloud session** (Linux container with Postgres 16 preinstalled, running as root; measured: schema in 2 s, 242
migrations in 6 s):

```bash
D=/var/lib/postgresql/fleettest; mkdir -p $D && chown postgres $D
su postgres -c "/usr/lib/postgresql/16/bin/initdb -D $D -U postgres -A trust"
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $D -o '-p 5433 -k /tmp' -l $D/pg.log start"
su postgres -c "psql -h /tmp -p 5433 -c 'CREATE ROLE test LOGIN SUPERUSER' -c 'CREATE DATABASE test OWNER test'"
redis-server --port 6380 --daemonize yes
```

CI runs Postgres 15: a finding that depends on the server version is re-run on a Postgres 15 machine before it counts.
Without any stack, run the sector's tests that need no database and say in the report which ones were skipped.

## 4. Frontend

```bash
npm ci --ignore-scripts --no-audit --no-fund                  # at the repo root; node_modules is git-ignored
cd apps/mouth
../../node_modules/.bin/tsc --noEmit                          # typecheck, whole app
npx vitest --run <file or directory>                          # targeted unit tests
```

On a 16 GB machine, skip a full `next build` (it can take 8-16 GB): `tsc` and targeted vitest are enough; the build is
CI's job.

## 5. Python packages

```bash
cd packages/cell-core && ../../apps/backend-rag/.venv/bin/python -m pytest tests -p no:cacheprovider
```

## 6. Local reproduction of the checks (run the ones your diff can affect, before any hand-over)

| Check | Command |
|---|---|
| Ownership | `python3 <kit>/resolve_sectors.py --repo . --diff origin/main...HEAD` lists only your sectors |
| Single writers | `python3 <kit>/check_keepers.py --role <none\|keeper_B\|keeper_F> --diff origin/main...HEAD` |
| Sovereignty lint | `git add -A && python3 scripts/check_ban_predicates.py --staged` and `python3 scripts/lint/lint_ban_prose.py --base-ref "$(git merge-base origin/main HEAD)"` |
| Perimeter | `python3 scripts/ci/perimeter_check.py` |
| Deploy gate lint (backend) | `cd apps/backend-rag && ruff check backend/app/ --select F401,F821,F822,F823 --no-cache` |
| API contract pin | `<kit>/t 'cd apps/backend-rag && PYTHONPATH=.:../crm-cell .venv/bin/python -m pytest backend/tests/unit/scripts/test_generate_openapi_schema_pin.py -p no:cacheprovider'` |
| Types (backend core) | `cd apps/backend-rag && PYTHONPATH=.:../crm-cell python -m mypy backend/core backend/app/models --config-file pyproject.toml` |
| Frontend | `cd apps/mouth && ../../node_modules/.bin/tsc --noEmit && npx vitest --run <your directories>` |
| Secrets | `pip install detect-secrets==1.5.0 && cp .secrets.baseline /tmp/b.json && detect-secrets scan --baseline /tmp/b.json <changed files> && python3 scripts/detect_secrets_check_unaudited.py --baseline /tmp/b.json` |
| Batch before a window (editor) | `bash <kit>/integration-rehearsal.sh <branch>... -- <guilt tests>`: merges the batch on `origin/main` and fails only on a check that is green there and red with the batch |

A new finding from the secrets scan is fixed by removing the secret-shaped literal, not by editing `.secrets.baseline`.

## 7. Machine notes, by class

- **Machine with a development database or production-adjacent services:** only the isolated worktree and the
  isolated stack; never touch its service agents, secret stores, other checkouts or development databases.
- **Machine that runs production schedulers:** one workflow at a time, `nice`, `-n 2`; the reproduce lens executes
  nothing under `scripts/` or `apps/backend-rag/scripts/` and makes no network calls. If a scheduler runs late, pause
  the fleet work there.
- **Laptop that saturates:** one workflow at a time, at most 4 agents, `-n 2`; re-run a red timing-sensitive test
  alone before believing it.
- **Windows laptop with WSL2:**
  - give WSL 10-12 GB in `%UserProfile%\.wslconfig` (`[wsl2]`, `memory=12GB`; on Windows 11 also
    `networkingMode=mirrored`);
  - keep the repository inside WSL (`~/rakyat`), never under `/mnt/c`, with `git config core.autocrlf false`;
  - Python 3.11 with `uv python install 3.11`, Node 24 with `fnm`, Claude Code installed natively inside WSL;
  - Tailscale inside WSL (or mirrored networking) so the coordinator can read the status files over ssh;
  - on mains power, set standby and lid-close to "do nothing";
  - use a Windows account dedicated to the fleet: no production credentials and no customer data on the machine;
  - a test that is red only because a script hard-codes `/Users/...` is noted, not chased (CI on Linux has the same
    behaviour).
- **Cloud session:** its own Postgres 16 and Redis (section 3); no private network, no push, no pull request; code
  and synthetic fixtures only; deliver `REPORT.md`, the ledger and `git format-patch` output in a private artifact at
  the end of every phase.
- **Phone or headset (control only):** no fleet work runs there. They paste the owner's prompts, read the board
  (published as a private artifact, or `ssh <coordinator> cat ~/.rakyat-fleet/BOARD.md` over the tailnet) and
  answer decision cards.
