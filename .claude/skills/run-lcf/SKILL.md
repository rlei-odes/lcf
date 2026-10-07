---
name: run-lcf
description: Launch and drive the lcf web application — the real app at its socket, not the test suite. Use this whenever the task is to run, start or serve lcf, to see the setup wizard, to stand up a throwaway or demo installation, to reset one back to fresh, or to confirm a change works in the browser rather than only in pytest. Also use it when asked to screenshot the app, walk a document end to end, or reproduce something a user reported on a page.
---

# Running lcf

lcf is one FastAPI service. `uv run lcf serve` starts it and that is nearly the
whole story — the interesting part is **which installation it starts as**, because
configuration lives in a file on disk and the repo's own `.env` points at the
real PostgreSQL and the real LLM endpoint.

So the first question is always: is this a throwaway, or the real one?

## Never launch a throwaway against the repo's `.env`

An installation is identified by two environment variables, and nothing else:

| Variable | What it decides |
|---|---|
| `LCF_ENV_FILE` | the config file read *and written* — the wizard saves here |
| `LCF_DATA_DIR` | where the SQLite file, uploads, exports and templates live |

Point both at a temp directory and you have an installation that cannot touch
the developer's own. Leave them unset and you are running *their* install,
against *their* database, and the wizard will happily write to their `.env`.

```bash
D=$(mktemp -d)
LCF_ENV_FILE=$D/.env LCF_DATA_DIR=$D uv run lcf serve --port 8099 --reload > $D/server.log 2>&1 &
```

`scripts/fresh.sh` does this, and the reset below, in one command — prefer it,
and read it if you need to do something it does not cover.

### Pass those two and nothing else

`LCF_*` variables exported into the process **outrank the config file**, and the
wizard knows it: `services/setup.py:shadowed` refuses to save a key that is set
in the environment, because saving it would have no effect and the app would go
on using the other value. Export `LCF_DB_URL` to "help" and you get a step that
cannot be completed and an error explaining why.

So when the wizard is meant to do the configuring, give it room:
`LCF_ENV_FILE` and `LCF_DATA_DIR` only. Those two are not wizard-writable keys,
so they shadow nothing.

## Port

The default is 8090, which is probably the developer's own instance. Use another
one — 8099 is a good habit — so a throwaway can never collide with something
they are already looking at.

## An unconfigured install serves the wizard, and only the wizard

There is no "empty app" state to debug. A middleware in `web/app.py` sends every
route to `/setup` until a database is configured, so a fresh launch lands on the
wizard. That is the front door: it asks for PostgreSQL or SQLite, tests the
connection before saving, runs the migrations, checks storage, tests the model
endpoint, and offers the two example document types at the end.

Walk it. It is quick, and it is what a real deployer sees — which makes it the
best way to find out that a step is broken.

- **SQLite** is the right choice for a throwaway: one file, nothing to install,
  and the path field defaults sensibly under `LCF_DATA_DIR`.
- **PostgreSQL** is what the product is built for. Choose it when the point is to
  reproduce something dialect-specific. The wizard generates the `psql` and
  `docker run` commands from what was typed.

Neither is preselected, and an installation that has not chosen is not
configured — so a fresh page genuinely starts from nothing.

Two things the wizard will not do, by design: it will not repoint a database that
is already live (`setup.guard` refuses; edit the config file and restart), and it
has no login, because this is normally deployed headless and the administrator is
always remote.

## Resetting to truly fresh

"Fresh" means the config file and the data directory are both gone. Deleting the
database alone leaves `LCF_DB_URL` behind, and the app starts configured and
broken instead of unconfigured.

```bash
S=.claude/skills/run-lcf/scripts/fresh.sh
$S --port 8099           # wipe and relaunch — the wizard starts from nothing
$S --port 8099 --keep    # relaunch, keep what is configured
$S --port 8099 --stop    # just stop it
```

The script stops the previous server before wiping, which matters: a server left
running recreates the SQLite file as soon as anything touches it, and the
installation is configured again before anyone has looked at it.

## Driving it

Clicking through the browser is the point when a human is watching. When you
need to check a change yourself, the app is a plain ASGI app and `TestClient`
drives it in-process with no server and no port:

```python
from fastapi.testclient import TestClient
from lcf.web.app import app
c = TestClient(app)
c.get("/setup")
c.post("/setup/database", data={"backend": "sqlite", "path": f"{D}/lcf.db"})
c.post("/setup/migrate")
```

Set the same two env vars before importing, since `settings()` resolves the
config path at import time.

This is how to verify HTMX swaps without a browser: the response to a step is a
fragment, and out-of-band updates arrive as sibling elements carrying
`hx-swap-oob`. Asserting on `re.findall(r'id="([^"]+)" hx-swap-oob', html)` tells
you exactly which parts of the page a step refreshed — and counting ids tells you
none were emitted twice.

`uv run lcf walkthrough` is the other way in: it drives a document end to end —
publish, fill, gate, revise, blame — with no model and no web server. Good for
proving the engine works before suspecting the UI.

## Reading the log

Write the server log to a file and `grep`/`tail` it. Piping uvicorn through
`tail` or `grep` in the same command buffers the output and you see nothing until
the process ends.

Wait for readiness on the log, not on a sleep:

```bash
until grep -q "Application startup complete" $D/server.log; do sleep 1; done
```

## Stopping it

```bash
.claude/skills/run-lcf/scripts/fresh.sh --port 8099 --stop
```

The script records the pid it started and kills that process group, which is the
only approach that survives this environment: `fuser`, `lsof`, `ss` and `netstat`
are all absent, so there is no way to map a port back to a process.

Do not reach for `pkill -f`. It matches whole command lines, so `pkill -f lcf`
issued from a shell whose own command line contains `lcf` kills that shell first
and the rest of the command never runs — it exits 144 and nothing is explained.
The same trap catches `pkill -f pytest` from a command that mentions pytest.

A port the script did not start is left alone, with a message, rather than
guessed at — that port probably belongs to the developer's own instance.

## Other commands worth knowing

| Command | What it does |
|---|---|
| `uv run lcf lint <spec.yaml>` | validate a document-type spec |
| `uv run lcf seed` | publish the example document types (same as the wizard's last step) |
| `uv run lcf publish <spec.yaml>` | publish one spec |
| `uv run lcf buckets` | create missing object-storage buckets |
| `uv run lcf walkthrough` | drive a document end to end, no LLM |

Install with `uv sync --extra dev`. The `dev` extra is only pytest and ruff;
`uv sync` alone is enough to serve.
