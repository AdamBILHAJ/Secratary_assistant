"use client";

import { useEffect, useState } from "react";

import { API_URL, type HealthResponse } from "@/lib/api";

import ScheduleAssistant from "./schedule-assistant";
import WordAssistant from "./word-assistant";

function formatUptime(seconds: number) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function StatusPill({
  online,
  label,
}: {
  online: boolean;
  label: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${
        online
          ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
          : "bg-rose-500/10 text-rose-300 ring-rose-500/30"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          online ? "bg-emerald-400" : "bg-rose-400"
        }`}
      />
      {label}
    </span>
  );
}

function Card({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 shadow-2xl shadow-black/20 backdrop-blur">
      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
        {eyebrow}
      </p>
      <h2 className="mt-1 text-base font-semibold text-slate-100">{title}</h2>
      <div className="mt-4">{children}</div>
    </section>
  );
}

export default function Home() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);

  const [reloadToken, setReloadToken] = useState(0);

  const refresh = () => {
    setLoading(true);
    setError(null);
    setReloadToken((token) => token + 1);
  };

  useEffect(() => {
    let cancelled = false;

    const checkHealth = async () => {
      try {
        const response = await fetch(`${API_URL}/api/health`, {
          cache: "no-store",
        });
        const body = (await response.json()) as HealthResponse;
        if (cancelled) return;

        setHealth(body);
        if (!response.ok && body.status !== "degraded") {
          setError(`API responded with HTTP ${response.status}`);
        }
      } catch {
        if (cancelled) return;
        setHealth(null);
        setError(`Cannot reach the API at ${API_URL}. Is uvicorn running?`);
      } finally {
        if (!cancelled) {
          setLoading(false);
          setCheckedAt(new Date().toLocaleTimeString());
        }
      }
    };

    void checkHealth();

    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  const apiOnline = health?.status === "ok";
  const mongo = health?.dependencies.mongodb;
  const mongoOnline = mongo?.status === "connected";

  return (
    <main className="relative min-h-screen overflow-hidden bg-slate-950 text-slate-100">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 left-1/2 h-[28rem] w-[48rem] -translate-x-1/2 rounded-full bg-indigo-500/20 blur-3xl"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -bottom-56 right-0 h-[24rem] w-[36rem] rounded-full bg-cyan-500/10 blur-3xl"
      />

      <div className="relative mx-auto flex min-h-screen w-full max-w-5xl flex-col px-6 py-10 sm:px-10">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/15 ring-1 ring-inset ring-indigo-400/30">
              <svg
                aria-hidden
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                className="h-5 w-5 text-indigo-300"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3.75 6.75A2.25 2.25 0 0 1 6 4.5h12a2.25 2.25 0 0 1 2.25 2.25v10.5A2.25 2.25 0 0 1 18 19.5H6a2.25 2.25 0 0 1-2.25-2.25V6.75Z"
                />
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M7.5 9h9M7.5 12h5.25M7.5 15h7.5"
                />
              </svg>
            </div>
            <div>
              <p className="text-sm font-semibold tracking-tight">
                IT Department Automation
              </p>
              <p className="text-xs text-slate-500">Operations console</p>
            </div>
          </div>
          <span className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-xs font-medium text-slate-400">
            v{health?.version ?? "0.1.0"}
          </span>
        </header>

        <section className="mt-16 max-w-2xl">
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-indigo-400">
            Full-stack starter
          </p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight text-white sm:text-5xl">
            Department operations,{" "}
            <span className="text-indigo-300">automated.</span>
          </h1>
          <p className="mt-4 max-w-xl text-base leading-7 text-slate-400">
            Next.js frontend, FastAPI backend, MongoDB Atlas persistence, and
            Google Gemini&apos;s document intelligence &mdash; wired together and
            ready for your workflows.
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <svg
                aria-hidden
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99"
                />
              </svg>
              {loading ? "Checking stack…" : "Re-run health check"}
            </button>
            <a
              href={`${API_URL}/docs`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-4 py-2 text-sm font-medium text-slate-300 transition hover:border-white/20 hover:text-white"
            >
              API docs
              <span aria-hidden>&#8599;</span>
            </a>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            {checkedAt ? `Last checked at ${checkedAt}` : "Checking services…"}
          </p>
        </section>

        {health?.status === "degraded" && !error && (
          <div
            role="status"
            className="mt-8 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200"
          >
            API is online, but MongoDB Atlas is unreachable. Check{" "}
            <code className="text-amber-100">MONGODB_URI</code> and your Atlas
            network access list.
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="mt-8 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200"
          >
            {error}
          </div>
        )}

        <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Card eyebrow="Backend" title="FastAPI service">
            <div className="flex items-center justify-between gap-3">
              <code className="truncate text-xs text-slate-400">{API_URL}</code>
              <StatusPill
                online={apiOnline}
                label={loading ? "Checking" : apiOnline ? "Online" : "Offline"}
              />
            </div>
            <dl className="mt-4 space-y-2 text-xs">
              <div className="flex justify-between gap-4">
                <dt className="text-slate-500">Health endpoint</dt>
                <dd className="text-slate-300">/api/health</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-500">Uptime</dt>
                <dd className="text-slate-300">
                  {health ? formatUptime(health.uptime_seconds) : "—"}
                </dd>
              </div>
            </dl>
          </Card>

          <Card eyebrow="Database" title="MongoDB Atlas">
            <div className="flex items-center justify-between gap-3">
              <code className="truncate text-xs text-slate-400">
                {mongo?.database ?? "it_automation"}
              </code>
              <StatusPill
                online={Boolean(mongoOnline)}
                label={
                  loading
                    ? "Checking"
                    : mongoOnline
                      ? "Connected"
                      : "Disconnected"
                }
              />
            </div>
            <dl className="mt-4 space-y-2 text-xs">
              <div className="flex justify-between gap-4">
                <dt className="text-slate-500">Ping latency</dt>
                <dd className="text-slate-300">
                  {typeof mongo?.latency_ms === "number"
                    ? `${mongo.latency_ms} ms`
                    : "—"}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-500">Driver</dt>
                <dd className="text-slate-300">PyMongo async</dd>
              </div>
            </dl>
            {mongo?.error && (
              <p className="mt-3 line-clamp-2 text-xs text-rose-300/80">
                {mongo.error}
              </p>
            )}
          </Card>

          <Card eyebrow="AI" title="Google Gemini">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-slate-400">google-genai</span>
              <span className="rounded-full bg-indigo-500/10 px-2.5 py-1 text-xs font-medium text-indigo-300 ring-1 ring-inset ring-indigo-500/30">
                Server-side
              </span>
            </div>
            <p className="mt-4 text-xs leading-6 text-slate-400">
              The API key stays on the backend. Add{" "}
              <code className="text-slate-300">GEMINI_API_KEY</code> to{" "}
              <code className="text-slate-300">backend/.env</code> to enable
              document extraction and summarisation.
            </p>
          </Card>
        </div>

        <ScheduleAssistant />
        <WordAssistant />

        <section className="mt-10 rounded-2xl border border-white/10 bg-white/[0.02] p-5">
          <h2 className="text-sm font-semibold text-slate-200">Project layout</h2>
          <div className="mt-4 grid gap-4 text-xs text-slate-400 sm:grid-cols-2">
            <div>
              <p className="font-medium text-slate-300">backend/</p>
              <ul className="mt-2 space-y-1.5 font-mono text-[11px]">
                <li>main.py — FastAPI app &amp; CORS</li>
                <li>database.py — Atlas async client</li>
                <li>requirements.txt</li>
                <li>.env.example</li>
              </ul>
            </div>
            <div>
              <p className="font-medium text-slate-300">frontend/</p>
              <ul className="mt-2 space-y-1.5 font-mono text-[11px]">
                <li>app/page.tsx — this dashboard</li>
                <li>lib/api.ts — typed API client</li>
                <li>app/layout.tsx — root layout</li>
                <li>.env.local.example</li>
              </ul>
            </div>
          </div>
        </section>

        <footer className="mt-auto flex flex-wrap items-center justify-between gap-3 pt-10 text-xs text-slate-600">
          <span>IT Department Automation App</span>
          <span>
            Documents ready: PDF · Excel · Word · Gemini-ready
          </span>
        </footer>
      </div>
    </main>
  );
}
