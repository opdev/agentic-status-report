# Agentic Weekly Status Pipeline

Automated weekly status reporting: collect activity from Jira and GitHub, draft
per-person entries with hosted Agent Skills, review in Slack, store in a Postgres
ledger, and synthesize a management report. Anthropic and OpenAI hosted Skills
are supported through the same pipeline.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

For local setup, Postgres, env vars, and the collect → draft → send dev loop, see
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Quick start

```bash
# Install Postgres (Fedora: dnf install postgresql-server, macOS: brew install postgresql)
# Start it: sudo systemctl start postgresql (or pg_ctl start on macOS)
createdb weekly_status

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[dev]"

export DATABASE_URL=postgresql+psycopg://localhost/weekly_status
alembic upgrade head

status --help
```

## CLI

```bash
status collect --person pilot --week 2026-08-14 --save-fixture
status draft   --fixture fixtures/pilot-2026-08-14.json --dry-run
status draft   --person pilot --week 2026-08-14
status send    --person pilot --week 2026-08-14
status slack run
status report  --week 2026-08-14 --dry-run
status skills list --provider openai
status skills publish --provider openai --skill all
```

Install Slack support: `pip install -e ".[slack]"`

## OpenShift (M3.5)

Deploy the Slack Socket Mode bot to OpenShift — no ingress required. See
[deploy/README.md](deploy/README.md) for build, secrets, and smoke-test steps.

## Project layout

```
skills/           Portable Agent Skill definitions
src/status/       Python pipeline
docs/             Design and development guides
alembic/          Database migrations
deploy/           Dockerfile and OpenShift manifests
fixtures/         Saved collector payloads for offline testing
```
