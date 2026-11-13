# Frontend

Next.js 16 (App Router) web app for the knowledge platform: auth, app shell, knowledge bases.

## Setup
```
npm ci
npm run dev          # http://localhost:3000
```

## Env
| Var | Default | Use |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend base URL. Read server-side by the BFF routes. |

## How auth works
The browser never sees tokens. `app/api/session/[action]` (login/register/logout) calls the backend and
stores `access_token` / `refresh_token` in httpOnly cookies (`rag_at`, `rag_rt`). `app/api/v1/[...path]`
proxies every API call to `${NEXT_PUBLIC_API_URL}/v1/*` with `Authorization: Bearer`, refreshing once on 401.
`proxy.ts` redirects to `/login` when neither cookie exists (optimistic check; the backend authorizes).

## Checks
```
npm run lint && npm run typecheck && npm test && npm run build
```
