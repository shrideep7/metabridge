# Developing MetaBridge in a Dev Container

The container is the development environment: your working tree is bind mounted
at `/workspace` and the package is installed **editable**, so code changes take
effect without rebuilding the image.

## Open it

1. Docker Desktop running, VS Code + the **Dev Containers** extension installed.
2. Open the repo in VS Code → `Ctrl+Shift+P` → **Dev Containers: Reopen in
   Container**. The first build takes a few minutes; later starts are quick.

## Run the app

From the container terminal (already at `/workspace`):

```bash
uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload \
  --reload-dir /workspace/web --reload-dir /workspace/src
```

Then open <http://localhost:8000/console>.

`--reload-dir` is passed explicitly on purpose: the app changes its working
directory to `METABRIDGE_DATA_DIR` at import time, so reload's default
"watch the current directory" behaviour is not reliable here.

Edits to `web/templates/console.html` are picked up on the next page load (the
template is read per request and served `no-store`), so no restart is needed —
only Python changes trigger a reload.

## Run the tests

```bash
pytest -q
```

## Configuration

`.env` at the repo root is loaded if present (it is gitignored — it holds real
credentials). Without it the app still starts; features that need a
credential report themselves as unconfigured. See `DEPLOYMENT.md` for the full
variable list.

Dev state (jobs, connections, the digital twin) lives in the
`metabridge-dev-data` volume — deliberately separate from the volume your
production container uses, so the two can never corrupt each other.

## Notes

- The dev container runs as **root** (the production image runs as the
  unprivileged `metabridge` user). That is what allows `pip install -e`.
- To reset dev state: `docker volume rm metabridge-dev-data` while the
  container is stopped.
- To pick up changes to `Dockerfile` or `pyproject.toml` dependencies:
  **Dev Containers: Rebuild Container**.
