"use client";

import { useState, type FormEvent } from "react";

import {
  askSchedule,
  type ScheduleAction,
  type ScheduleAskResponse,
  type ScheduleSession,
} from "@/lib/api";

const QUICK_EXAMPLES = [
  {
    label: "Check a room",
    prompt: "Is room C9 free on Monday at 10:15-11:45?",
  },
  {
    label: "Find free rooms",
    prompt: "Find available rooms on Tuesday from 8h30 to 10h00 in Q1.",
  },
  {
    label: "Ask about a class",
    prompt: "Which subjects does teacher Dupont teach in Q2?",
  },
  {
    label: "Ask about a schedule",
    prompt: "What classes are scheduled on Wednesday afternoon for L1-TI-1?",
  },
];

const ACTION_LABELS: Record<ScheduleAction, string> = {
  check_room_availability: "Room availability",
  find_available_rooms: "Free-room search",
  query_schedule: "Schedule question",
};

const FILTER_LABELS: Record<string, string> = {
  room: "Room",
  day: "Day",
  date: "Date",
  time_slot: "Time",
  week_badge: "Badge",
  group: "Group",
  class_name: "Class",
  teacher: "Teacher",
  subject: "Subject",
  active_week: "Active week",
};

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function sessionRows(session: ScheduleSession) {
  return {
    day: displayValue(session.day),
    time: displayValue(session.time_slot ?? `${session.start_time ?? ""}-${session.end_time ?? ""}`),
    class_name: displayValue(session.class_name),
    subject: displayValue(session.subject),
    teacher: displayValue(session.teacher),
    room: displayValue(session.room),
    week: displayValue(session.week ?? session.group),
  };
}

export default function ScheduleAssistant() {
  const [prompt, setPrompt] = useState("");
  const [room, setRoom] = useState("");
  const [day, setDay] = useState("");
  const [time, setTime] = useState("");
  const [badge, setBadge] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScheduleAskResponse | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      setError("Ask a question about a room or the timetable.");
      return;
    }

    const hints = [
      room.trim() && `room ${room.trim()}`,
      day.trim() && `day ${day.trim()}`,
      time.trim() && `time ${time.trim()}`,
      badge.trim() && `badge ${badge.trim()}`,
    ].filter((value): value is string => Boolean(value));

    const finalPrompt = hints.length
      ? `${cleanPrompt}\nContext: ${hints.join("; ")}.`
      : cleanPrompt;

    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await askSchedule(finalPrompt));
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not answer the schedule question.",
      );
    } finally {
      setLoading(false);
    }
  };

  const filterEntries = Object.entries(result?.filters ?? {}).filter(
    ([key, value]) =>
      key in FILTER_LABELS &&
      value !== null &&
      value !== undefined &&
      value !== "" &&
      key !== "confidence" &&
      !Array.isArray(value),
  );

  const sessions = result?.matching_sessions ?? [];

  return (
    <section className="mt-10 rounded-2xl border border-white/10 bg-white/[0.03] p-5 shadow-2xl shadow-black/20 backdrop-blur sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-400">
            Schedules
          </p>
          <h2 className="mt-1 text-lg font-semibold text-slate-100">
            Room &amp; Schedule Assistant
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">
            Ask one question in plain language. The assistant can check a room,
            list free rooms, or answer open questions about classes, teachers,
            subjects and alternating weeks.
          </p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-300">
          MongoDB read-only
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
            htmlFor="schedule-prompt"
            className="text-xs font-medium text-slate-300"
          >
            Your question
          </label>
          <textarea
            id="schedule-prompt"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={3}
            placeholder="Example: Is room C9 free on Monday at 10h15-11h45?"
            className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950/60 px-3.5 py-3 text-sm leading-6 text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-indigo-400/60 focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[
            {
              id: "schedule-room",
              label: "Room",
              value: room,
              setter: setRoom,
              placeholder: "C9",
            },
            {
              id: "schedule-day",
              label: "Day",
              value: day,
              setter: setDay,
              placeholder: "lundi",
            },
            {
              id: "schedule-time",
              label: "Time",
              value: time,
              setter: setTime,
              placeholder: "10:15-11:45",
            },
            {
              id: "schedule-badge",
              label: "Badge",
              value: badge,
              setter: setBadge,
              placeholder: "Q1 / Q2",
            },
          ].map((field) => (
            <div key={field.id}>
              <label
                htmlFor={field.id}
                className="text-[11px] font-medium text-slate-400"
              >
                {field.label} <span className="text-slate-600">(optional)</span>
              </label>
              <input
                id={field.id}
                value={field.value}
                onChange={(event) => field.setter(event.target.value)}
                placeholder={field.placeholder}
                className="mt-1.5 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2 text-sm text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-indigo-400/60 focus:ring-2 focus:ring-indigo-500/20"
              />
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading ? "Checking…" : "Ask assistant"}
          </button>
          <p className="text-[11px] text-slate-500">
            Optional fields are added as hints to your prompt.
          </p>
        </div>
      </form>

      {error && (
        <div
          role="alert"
          className="mt-5 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200"
        >
          {error}
        </div>
      )}

      {result && (
        <div className="mt-6 space-y-4 border-t border-white/10 pt-5">
          <div className="rounded-xl border border-white/10 bg-slate-950/40 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-indigo-500/10 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide text-indigo-300 ring-1 ring-inset ring-indigo-500/30">
                {ACTION_LABELS[result.action]}
              </span>
              {result.available !== null && (
                <span
                  className={`rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset ${
                    result.available
                      ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                      : "bg-rose-500/10 text-rose-300 ring-rose-500/30"
                  }`}
                >
                  {result.available ? "Available" : "Occupied"}
                </span>
              )}
            </div>
            <p className="mt-3 text-sm leading-6 text-slate-200">{result.answer}</p>
            {result.routing_note && (
              <p className="mt-2 text-xs text-amber-300/80">{result.routing_note}</p>
            )}
            {filterEntries.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {filterEntries.map(([key, value]) => (
                  <span
                    key={key}
                    className="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[11px] text-slate-400"
                  >
                    {FILTER_LABELS[key]}: {displayValue(value)}
                  </span>
                ))}
              </div>
            )}
          </div>

          {result.available_rooms.length > 0 && (
            <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/[0.06] p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-emerald-300">
                Free rooms
              </h3>
              <div className="mt-3 flex flex-wrap gap-2">
                {result.available_rooms.map((roomCode) => (
                  <span
                    key={roomCode}
                    className="rounded-lg border border-emerald-400/20 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-200"
                  >
                    {roomCode}
                  </span>
                ))}
              </div>
            </div>
          )}

          {result.conflicts.length > 0 && (
            <div className="rounded-xl border border-rose-500/20 bg-rose-500/[0.06] p-4">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-rose-300">
                Conflicts
              </h3>
              <ul className="mt-2 space-y-1.5 text-xs text-rose-100/90">
                {result.conflicts.slice(0, 8).map((conflict, index) => (
                  <li key={conflict.entry_key ?? index}>
                    {conflict.room} — {conflict.subject ?? "Session"} (
                    {conflict.time_slot ?? "time unknown"})
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="rounded-xl border border-white/10 bg-slate-950/30 p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              Matching timetable rows
            </h3>
            {sessions.length === 0 ? (
              <p className="mt-3 text-xs text-slate-500">
                No schedule records matched this question.
              </p>
            ) : (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-xs">
                  <thead className="text-slate-500">
                    <tr>
                      <th className="py-2 pr-3 font-medium">Day</th>
                      <th className="py-2 pr-3 font-medium">Time</th>
                      <th className="py-2 pr-3 font-medium">Class</th>
                      <th className="py-2 pr-3 font-medium">Subject</th>
                      <th className="py-2 pr-3 font-medium">Teacher</th>
                      <th className="py-2 pr-3 font-medium">Room</th>
                      <th className="py-2 font-medium">Week</th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {sessions.slice(0, 20).map((session, index) => {
                      const row = sessionRows(session);
                      return (
                        <tr
                          key={session.entry_key ?? index}
                          className="border-t border-white/5"
                        >
                          <td className="py-2 pr-3 capitalize">{row.day}</td>
                          <td className="py-2 pr-3">{row.time}</td>
                          <td className="py-2 pr-3">{row.class_name}</td>
                          <td className="py-2 pr-3">{row.subject}</td>
                          <td className="py-2 pr-3">{row.teacher}</td>
                          <td className="py-2 pr-3">{row.room}</td>
                          <td className="py-2">{row.week}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
