export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export type MongoHealth = {
  status: "connected" | "disconnected";
  database: string;
  latency_ms: number;
  error?: string;
};

export type HealthResponse = {
  status: "ok" | "degraded";
  service: string;
  version: string;
  timestamp: string;
  uptime_seconds: number;
  dependencies: {
    mongodb: MongoHealth;
  };
};

export type BlueprintTableSummary = {
  index: number;
  header: string[];
  row_count: number;
  column_count: number;
  kind: string;
};

export type BlueprintStructure = {
  headings: string[];
  tables: BlueprintTableSummary[];
  paragraph_count: number;
  table_count: number;
};

export type BlueprintSummary = {
  id: string;
  name: string;
  filename: string;
  title: string;
  description: string;
  source: string;
  location: string;
  size_bytes: number;
  modified_at: string;
  structure: BlueprintStructure;
};

export type BlueprintListResponse = {
  count: number;
  allowlisted: boolean;
  blueprints: BlueprintSummary[];
  upload_directory: string;
};

export type GenerateWordInput = {
  prompt: string;
  target_class?: string;
  blueprint_id?: string;
  output_name?: string;
};

export type GeneratedWordDocument = {
  blob: Blob;
  filename: string;
  summary: string;
  explanation: string;
};

async function errorMessage(response: Response): Promise<string> {
  const fallback = `Request failed with HTTP ${response.status}`;
  try {
    const text = (await response.text()).trim();
    if (!text) return fallback;
    try {
      const body = JSON.parse(text) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        return body.detail;
      }
    } catch {
      // Plain-text error body.
    }
    return text;
  } catch {
    return fallback;
  }
}

function decodeHeader(value: string | null): string {
  if (!value) return "";
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function filenameFromDisposition(
  disposition: string | null,
  fallback: string,
): string {
  if (!disposition) return fallback;

  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded?.[1]) return decodeHeader(encoded[1]);

  const quoted = disposition.match(/filename="([^"]+)"/i);
  if (quoted?.[1]) return quoted[1];

  return fallback;
}

export async function fetchBlueprints(
  signal?: AbortSignal,
): Promise<BlueprintListResponse> {
  const response = await fetch(`${API_URL}/api/documents/blueprints`, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return (await response.json()) as BlueprintListResponse;
}

export async function generateWordDocument(
  input: GenerateWordInput,
  signal?: AbortSignal,
): Promise<GeneratedWordDocument> {
  const response = await fetch(`${API_URL}/api/documents/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      doc_type: "word",
      prompt: input.prompt,
      target_class: input.target_class || null,
      blueprint_id: input.blueprint_id || null,
      output_name: input.output_name || null,
    }),
    signal,
  });

  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }

  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition");
  return {
    blob,
    filename: filenameFromDisposition(disposition, "document.docx"),
    summary: decodeHeader(response.headers.get("X-Doc-Summary")),
    explanation: decodeHeader(response.headers.get("X-Doc-Explanation")),
  };
}
