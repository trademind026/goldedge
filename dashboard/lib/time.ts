/**
 * Timestamp timezone helpers.
 *
 * The backend correctly stores and returns true UTC timestamps.
 * This library properly parses them and converts them into Asia/Kolkata local time strings
 * for display in the UI.
 */

/** Helper to parse a UTC string from the backend into a Javascript Date object. */
function parseUTC(ts: string): Date {
  if (!ts) return new Date(NaN);
  let s = ts.trim();
  // Convert "2026-06-25 03:45:00" to "2026-06-25T03:45:00Z"
  if (s.length === 19 && s[10] === ' ') {
    s = s.replace(' ', 'T') + 'Z';
  } else if (!s.endsWith('Z') && !s.includes('+') && !s.includes('-')) {
    s += 'Z';
  }
  return new Date(s);
}

/** Return HH:MM in IST for a timestamp string. */
export function toISTTime(ts: string): string {
  if (!ts) return "--";
  const d = parseUTC(ts);
  if (isNaN(d.getTime())) return ts.substring(11, 16);
  return d.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: '2-digit', minute: '2-digit', hour12: false });
}

/** Return HH:MM:SS in IST for a timestamp string. */
export function toISTTimeFull(ts: string): string {
  if (!ts) return "--";
  const d = parseUTC(ts);
  if (isNaN(d.getTime())) return ts.substring(11, 19);
  return d.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

/** Return YYYY-MM-DD date portion in IST. */
export function toDateStr(ts: string): string {
  if (!ts) return "--";
  const d = parseUTC(ts);
  if (isNaN(d.getTime())) return ts.substring(0, 10);
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }); // en-CA gives YYYY-MM-DD
}
