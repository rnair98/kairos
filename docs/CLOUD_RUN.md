# Cloud Run deployment

Kairos ships a slim Docker image (`Dockerfile`) that uses **Gemini API embeddings** — no `sentence-transformers` in the container.

## Build and deploy

```bash
gcloud run deploy kairos \
  --source . \
  --region us-central1 \
  --min-instances 1 \
  --memory 512Mi \
  --set-env-vars "EMBEDDING_BACKEND=gemini,VECTOR_SEARCH_ENABLED=true" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,TURSO_DATABASE_URL=turso-database-url:latest,TURSO_AUTH_TOKEN=turso-auth-token:latest"
```

Required env vars:

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | LLM + embeddings |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Hosted Turso primary — omit both for a purely local `kairos.db` file (fine for single-instance Cloud Run with a mounted volume; use a hosted primary for multi-instance) |
| `KAIROS_USER_ID` | Active user after Google OAuth |
| `EMBEDDING_BACKEND` | `gemini` (default in container) |

## libSQL vector search

Vector columns (`clusters.centroid_embedding`, `bookmarks.embedding`) are native libSQL `F32_BLOB` columns — no separate index-creation step, unlike Atlas. Dimensions must match `GEMINI_EMBEDDING_DIMENSIONS` (default 768). If the libSQL build lacks vector functions, ranking falls back to in-memory cosine similarity.

Disable vector search: `VECTOR_SEARCH_ENABLED=false`

## Local dev with offline embeddings

```bash
uv sync --extra local
EMBEDDING_BACKEND=local kairos bookmarks embed && kairos bookmarks cluster
```

Re-embed after switching between `gemini` and `local` — vector spaces are incompatible.
