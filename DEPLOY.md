# Study Sprint Deployment

## Recommended path

Use one service to host both the static page and the Python API.

Why:

- `AI_API_KEY` stays on the server.
- Frontend and API share the same origin.
- No CORS mess.
- No need to split Cloudflare Pages + another backend.

## What changed in this repo

- `server.py` now reads `HOST` and defaults to `0.0.0.0`.
- `.env` is ignored by git.
- The frontend now calls the current origin instead of hard-coded localhost.

## Minimal deploy checklist

1. Create a new Git repo for this folder.
2. Push it to GitHub.
3. Deploy it to a Python-friendly host like Render or Railway.
4. Set the start command to `python server.py`.
5. Add these environment variables in the platform dashboard, not in git:
   - `AI_API_KEY`
   - `AI_API_BASE`
   - `AI_API_MODEL`
   - `PORT`
   - `HOST=0.0.0.0`
6. Do not upload `.env`.

## Optional hardening

- Turn on `REQUIRE_APP_TOKEN` only if you want a closed beta.
- Keep rate limiting enabled.
- Add a custom domain after health check passes.

## Smoke test

- Open `/api/health`
- Open `/mvp-study-sprint.html`
- Submit one real request
- Confirm the browser network panel does not expose `AI_API_KEY`
