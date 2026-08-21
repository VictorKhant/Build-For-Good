# BellyUp SQLite database

`bellyup.db` is the runtime data store used by optimization and FastAPI.
Rebuild it from the preserved CSV snapshots with:

```bash
python3 calc/build_database.py
```

CSV source snapshots are retained under `calc/original_files/`.
