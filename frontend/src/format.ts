// Display helpers. A missing value is always shown as missing, never as 0.
export const TZ = "Asia/Kolkata";

const dateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, day: "2-digit", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false,
});
const shortTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});
const hourOnly = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false,
});

function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "25 Oct 2026, 21:45 IST" */
export function fmtTime(iso: string | null | undefined): string {
  const d = toDate(iso);
  return d ? `${dateTime.format(d)} IST` : "—";
}

export function fmtShort(iso: string | null | undefined): string {
  const d = toDate(iso);
  return d ? shortTime.format(d) : "—";
}

export function fmtHour(iso: string | null | undefined): string {
  const d = toDate(iso);
  return d ? hourOnly.format(d) : "";
}

export function epoch(iso: string | null | undefined): number | null {
  const d = toDate(iso);
  return d ? d.getTime() : null;
}

/** Number with unit, or a dash when the value is missing or not finite. */
export function fmtNum(value: number | null | undefined, unit = "", digits = 1): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function fmtAge(hours: number | null | undefined): string {
  if (!isNum(hours)) return "unknown age";
  if (hours < 1) return `${Math.round(hours * 60)} min old`;
  if (hours < 48) return `${hours.toFixed(1)} h old`;
  return `${(hours / 24).toFixed(1)} days old`;
}

/** "EVIDENCE_STALE" -> "Evidence stale" */
export function humanize(code: string | null | undefined): string {
  if (!code) return "—";
  const text = code.replace(/^R\d+_/, "").replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export const RISK_TONE: Record<string, string> = {
  LOW: "ok", MODERATE: "caution", HIGH: "warn", CRITICAL: "danger", INDETERMINATE: "unknown",
};
export const STATE_TONE: Record<string, string> = {
  NORMAL: "ok", ALERT: "warn", EMERGENCY: "danger", RECOVERY: "info",
};
export const STATE_ORDER = ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"];
