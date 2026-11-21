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
