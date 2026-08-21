# BellyUp FastAPI

Start the API from the repository root:

```bash
python3 -m uvicorn api.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000/docs` for interactive Swagger documentation.

Main endpoints:

- `GET /health`
- `GET /tables`
- `GET /data/{table_name}?limit=100`
- `POST /optimize/standard`
- `POST /optimize/simulation`
- `GET /results/{mode}/{method}` where method is `greedy` or `optimized`

The API reads inputs and optimization outputs from `calc/sql/bellyup.db`.
