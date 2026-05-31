# Experiment Registry

Local-first SQLite registry for research coordination.

Default live database:

```text
registry/experiments.sqlite
```

Tracked:

- `registry/schema.sql`
- `registry/store.py`
- `scripts/experiment_registry.py`

Ignored:

- the live SQLite file and its journal/WAL sidecars

Typical usage:

```bash
python scripts/experiment_registry.py init
python scripts/experiment_registry.py summary
python scripts/experiment_registry.py thread upsert --name layerscale --status active
python scripts/experiment_registry.py queue upsert --thread-name layerscale --name screen5m --command "python train_llm.py ..."
python scripts/experiment_registry.py idea import-known-levers --path /path/to/llm-research-kit-scaling/docs/KNOWN_LEVERS.md
```

The Streamlit dashboard at `registry/dashboard.py` supports approving ideas in the DB
and promoting approved ideas into the queue when they have a runnable command.

The database is for coordination. The evidence still lives in Git tags, metrics files, and notes.
