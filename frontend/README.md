# Frontend — IT Department Automation

Next.js 16 (App Router, TypeScript, Tailwind CSS) dashboard for the API.

```bash
npm install
npm run dev
```

The app runs at <http://localhost:3000> and calls the backend health endpoint
at `GET /api/health`.

## Configuration

Copy `.env.local.example` to `.env.local` if your API is not on
`http://localhost:8000`:

```dotenv
NEXT_PUBLIC_API_URL=http://localhost:8000
```

See the root [`README.md`](../README.md) for full-stack setup instructions.
