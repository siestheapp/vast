# Vast1 Frontend Prototype

A minimal browser UI that talks to the Vast1 FastAPI backend.

## Prerequisites

- Run the API server first: `uvicorn src.vast.api:app --reload`
- Ensure the `.env` file is configured with `DATABASE_URL` and `OPENAI_API_KEY`

## Launch

```bash
python -m http.server --directory frontend 5173
```

Open <http://localhost:5173> and use the controls to:

- Check backend health
- Ask the agent natural-language questions
- Execute raw SQL safely with guardrails
- Browse schema tables and columns
- View generated artifacts (e.g., database dumps)

Update the API base URL field in the header if your server is running on a different host or port.
