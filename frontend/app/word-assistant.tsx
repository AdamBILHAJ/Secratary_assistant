"use client";

import { useEffect, useState, type FormEvent } from "react";

import {
  fetchBlueprints,
  generateWordDocument,
  type BlueprintSummary,
} from "@/lib/api";

const QUICK_EXAMPLES = [
  {
    label: "Update a template",
    prompt:
      "Use the latest Liste des Besoins template and update the date to today.",
  },
  {
    label: "Edit the needs list",
    prompt:
      "Add 10 HDMI cables and remove scotch from the Liste des Besoins, date 22/11/2026.",
  },
  {
    label: "Create a service note",
    prompt:
      "Create a service note about lab maintenance, with a short introduction and next steps.",
  },
  {
    label: "Draft a class schedule",
    prompt:
      "Create a schedule note for the class, with a clear timetable and notes section.",
  },
];

type GeneratedState = {
  url: string;
  filename: string;
  summary: string;
  sizeBytes: number;
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function WordAssistant() {
  const [prompt, setPrompt] = useState("");
  const [targetClass, setTargetClass] = useState("");
  const [blueprintId, setBlueprintId] = useState("");

  const [blueprints, setBlueprints] = useState<BlueprintSummary[]>([]);
  const [blueprintsLoading, setBlueprintsLoading] = useState(true);
  const [blueprintsError, setBlueprintsError] = useState<string | null>(null);
  const [blueprintReload, setBlueprintReload] = useState(0);

  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generated, setGenerated] = useState<GeneratedState | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    fetchBlueprints(controller.signal)
      .then((response) => {
        if (cancelled) return;
        setBlueprints(response.blueprints);
      })
      .catch((reason: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        setBlueprints([]);
        setBlueprintsError(
          reason instanceof Error
            ? reason.message
            : "Could not load the allowlisted templates.",
        );
      })
      .finally(() => {
        if (!cancelled) setBlueprintsLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [blueprintReload]);

  useEffect(() => {
    return () => {
      if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    };
  }, [downloadUrl]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      setError("Describe the Word document you need.");
      return;
    }

    setGenerating(true);
    setError(null);
    setGenerated(null);
    setDownloadUrl(null);

    try {
      const document = await generateWordDocument({
        prompt: cleanPrompt,
        target_class: targetClass.trim() || undefined,
        blueprint_id: blueprintId || undefined,
      });
      const url = URL.createObjectURL(document.blob);
      setDownloadUrl(url);
      setGenerated({
        url,
        filename: document.filename,
        summary:
          document.summary ||
          "Your Word document is ready to download.",
        sizeBytes: document.blob.size,
      });
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not generate the Word document.",
      );
    } finally {
      setGenerating(false);
    }
  };

  return (
    <section className="mt-10 rounded-2xl border border-white/10 bg-white/[0.03] p-5 shadow-2xl shadow-black/20 backdrop-blur sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-400">
            Word documents
          </p>
          <h2 className="mt-1 text-lg font-semibold text-slate-100">
            Word Document Assistant
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">
            Describe what you need in plain language. The assistant can use an
            approved template, adapt one with targeted changes, or build a clean
            new document.
          </p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-xs font-medium text-slate-400">
          .docx
        </span>
      </div>

      <div className="mt-5 flex flex-wrap gap-2">
        {QUICK_EXAMPLES.map((example) => (
          <button
            key={example.label}
            type="button"
            onClick={() => setPrompt(example.prompt)}
            className="rounded-full border border-white/10 bg-white/[0.02] px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-indigo-400/40 hover:bg-indigo-500/10 hover:text-indigo-200"
          >
            {example.label}
          </button>
        ))}
      </div>

      <form onSubmit={handleSubmit} className="mt-5 space-y-4">
        <div>
          <label
            htmlFor="word-prompt"
            className="text-xs font-medium text-slate-300"
          >
            What should the document contain?
          </label>
          <textarea
            id="word-prompt"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={5}
            placeholder="Example: Use the Liste des Besoins template, update the date to today, and add 10 HDMI cables."
            className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950/60 px-3.5 py-3 text-sm leading-6 text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-indigo-400/60 focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label
              htmlFor="word-target-class"
              className="text-xs font-medium text-slate-300"
            >
              Class <span className="text-slate-600">(optional)</span>
            </label>
            <input
              id="word-target-class"
              value={targetClass}
              onChange={(event) => setTargetClass(event.target.value)}
              placeholder="e.g. L1-TI-1"
              className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3.5 py-2.5 text-sm text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-indigo-400/60 focus:ring-2 focus:ring-indigo-500/20"
            />
            <p className="mt-1.5 text-[11px] text-slate-500">
              Enriches a new document with the class schedule when available.
            </p>
          </div>

          <div>
            <label
              htmlFor="word-blueprint"
              className="text-xs font-medium text-slate-300"
            >
              Template <span className="text-slate-600">(optional)</span>
            </label>
            <select
              id="word-blueprint"
              value={blueprintId}
              onChange={(event) => setBlueprintId(event.target.value)}
              disabled={blueprintsLoading || blueprints.length === 0}
              className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950/60 px-3.5 py-2.5 text-sm text-slate-100 outline-none transition focus:border-indigo-400/60 focus:ring-2 focus:ring-indigo-500/20 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <option value="">
                {blueprintsLoading
                  ? "Loading templates…"
                  : "Auto (choose template or generate from scratch)"}
              </option>
              {blueprints.map((blueprint) => (
                <option key={blueprint.id} value={blueprint.id}>
                  {blueprint.title}
                </option>
              ))}
            </select>
            <p className="mt-1.5 text-[11px] text-slate-500">
              {blueprintsError
                ? "Templates are unavailable; the assistant can still create a new document."
                : `${blueprints.length} approved template${
                    blueprints.length === 1 ? "" : "s"
                  } available.`}
            </p>
          </div>
        </div>

        {blueprintsError && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/25 bg-amber-500/10 px-3.5 py-2.5 text-xs text-amber-200">
            <span>{blueprintsError}</span>
            <button
              type="button"
              onClick={() => {
                setBlueprintsLoading(true);
                setBlueprintsError(null);
                setBlueprintReload((token) => token + 1);
              }}
              className="font-semibold text-amber-100 underline decoration-amber-300/50 underline-offset-2 hover:text-white"
            >
              Retry
            </button>
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3.5 py-2.5 text-sm text-rose-200"
          >
            {error}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={generating}
            className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {generating ? (
              <>
                <svg
                  aria-hidden
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  className="h-4 w-4 animate-spin"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99"
                  />
                </svg>
                Preparing document…
              </>
            ) : (
              <>
                <svg
                  aria-hidden
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  className="h-4 w-4"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M12 4.5v15m7.5-7.5h-15"
                  />
                </svg>
                Generate Word document
              </>
            )}
          </button>
          <p className="text-xs text-slate-500">
            The original templates are never modified.
          </p>
        </div>
      </form>

      {generated && (
        <div
          role="status"
          className="mt-5 rounded-xl border border-emerald-500/25 bg-emerald-500/[0.07] p-4"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-300">
                Document ready
              </p>
              <p className="mt-1.5 text-sm leading-6 text-slate-200">
                {generated.summary}
              </p>
              <p className="mt-2 truncate text-[11px] text-slate-500">
                {generated.filename} · {formatBytes(generated.sizeBytes)}
              </p>
            </div>
            <a
              href={generated.url}
              download={generated.filename}
              className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400"
            >
              <svg
                aria-hidden
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.9"
                className="h-4 w-4"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"
                />
              </svg>
              Download .docx
            </a>
          </div>
        </div>
      )}
    </section>
  );
}
