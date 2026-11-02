import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router";

import type { BackupRun, Backups, Service, ServiceConfigFile } from "../api/compliance";
import { saveDownload } from "../api/exports";
import { linkClass, secondaryClass } from "../components/styles";
import { cardClass, CopyValue } from "../settings/parts";
import { formatTime } from "../time";
import { Markdown } from "./Markdown";

const cell = "px-3 py-2 align-top";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

type Platform = "compose" | "helm";

const platforms: { id: Platform; label: string }[] = [
  { id: "compose", label: "Docker Compose" },
  { id: "helm", label: "Kubernetes (Helm)" },
];

function Badge({ tone, children }: { tone: "good" | "bad" | "neutral"; children: ReactNode }) {
  const tones = {
    good: "bg-emerald-100 text-emerald-900",
    bad: "bg-red-100 text-red-900",
    neutral: "bg-slate-100 text-slate-700",
  };
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/** "Running and healthy", "Running, not healthy" or "Not detected". */
export function serviceState(service: Service): {
  label: string;
  tone: "good" | "bad" | "neutral";
} {
  if (service.detected && service.healthy) return { label: "Running and healthy", tone: "good" };
  if (service.detected) return { label: "Running, not healthy", tone: "bad" };
  return { label: "Not detected", tone: service.required ? "bad" : "neutral" };
}

function download(file: ServiceConfigFile) {
  const blob = new Blob([file.content], { type: "text/plain;charset=utf-8" });
  saveDownload({ blob, filename: file.file_name, bytes: blob.size, exportId: null });
}

/** Docker Compose and Helm instructions as tabs, with the generated configuration to download. */
function HowTo({ service }: { service: Service }) {
  const [active, setActive] = useState<Platform>("compose");
  const id = useId();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const next =
      (index + (event.key === "ArrowRight" ? 1 : -1) + platforms.length) % platforms.length;
    const platform = platforms[next];
    if (!platform) return;
    setActive(platform.id);
    tabs.current[next]?.focus();
  }

  const file = service.config?.[active];
  return (
    <div className="space-y-3">
      <div role="tablist" aria-label={`How to run ${service.name}`} className="flex gap-1">
        {platforms.map((platform, index) => {
          const selected = platform.id === active;
          return (
            <button
              key={platform.id}
              ref={(element) => {
                tabs.current[index] = element;
              }}
              type="button"
              role="tab"
              id={`${id}-${platform.id}-tab`}
              aria-selected={selected}
              aria-controls={`${id}-${platform.id}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => {
                setActive(platform.id);
              }}
              onKeyDown={(event) => {
                onKeyDown(event, index);
              }}
              className={`rounded-t border-b-2 px-3 py-1.5 text-sm font-medium ${
                selected
                  ? "border-sky-700 text-sky-900"
                  : "border-transparent text-slate-600 hover:text-slate-900"
              }`}
            >
              {platform.label}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`${id}-${active}-panel`}
        aria-labelledby={`${id}-${active}-tab`}
        className="space-y-3"
      >
        <Markdown
          source={service.how_to[active]}
          label={`${service.name}, ${platforms.find((item) => item.id === active)?.label ?? ""}`}
        />
        {file && (
          <button
            type="button"
            onClick={() => {
              download(file);
            }}
            className={secondaryClass}
          >
            Download {file.file_name}
          </button>
        )}
      </div>
    </div>
  );
}

function ServiceItem({ service }: { service: Service }) {
  const state = serviceState(service);
  return (
    <li className="space-y-2 border-b border-slate-200 py-3 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <Badge tone={state.tone}>{state.label}</Badge>
        <h3 className="font-medium">{service.name}</h3>
        <span className="text-xs text-slate-600">
          {service.required ? "Required by the profile" : "Optional"}
        </span>
      </div>
      <p className="text-sm text-slate-700">{service.purpose}</p>
      {service.detail && <p className="text-sm text-slate-700">{service.detail}</p>}
      {service.id === "audit-shipping" && (
        <p className="text-sm">
          Runs inside Baluarte; nothing to start.{" "}
          <Link to="/settings#audit-shipping" className={linkClass}>
            Configure audit log shipping in Settings
          </Link>
        </p>
      )}
      <details
        className="text-sm"
        open={service.required && !(service.detected && service.healthy)}
      >
        <summary className="cursor-pointer font-medium text-sky-800">
          How to set it up
          <span className="sr-only">: {service.name}</span>
        </summary>
        <div className="mt-2">
          <HowTo service={service} />
        </div>
      </details>
    </li>
  );
}

/** Services Baluarte relies on but cannot start itself, with the steps to start them. */
export function ServicesSection({ services }: { services: Service[] }) {
  return (
    <section aria-labelledby="services-heading" className={cardClass}>
      <h2 id="services-heading" className="text-lg font-semibold">
        Services
      </h2>
      <p className="text-sm text-slate-600">
        Some controls need a service running beside Baluarte. Baluarte cannot start containers
        itself, since that would need control of the host; it checks whether each service is working
        and shows how to start it.
      </p>
      {services.length === 0 ? (
        <p className="text-sm text-slate-700">No services are needed.</p>
      ) : (
        <ul aria-label="Services" className="space-y-0">
          {services.map((service) => (
            <ServiceItem key={service.id} service={service} />
          ))}
        </ul>
      )}
    </section>
  );
}

function duration(run: BackupRun): string {
  const seconds = Math.max(
    0,
    Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000),
  );
  if (Number.isNaN(seconds)) return "";
  if (seconds < 60) return `${String(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes)} min ${String(seconds % 60)} s`;
}

function Sha({ value }: { value: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);

  function copy() {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (!clipboard) {
      setCopied("no");
      return;
    }
    clipboard.writeText(value).then(
      () => {
        setCopied("yes");
      },
      () => {
        setCopied("no");
      },
    );
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <code title={value} className="font-mono text-xs">
        {value.slice(0, 12)}…
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy SHA-256 ${value}`}
        className="text-xs text-sky-800 underline"
      >
        Copy
      </button>
      <span aria-live="polite" className="text-xs text-slate-600">
        {copied === "yes" ? "Copied." : copied === "no" ? "Copy failed." : ""}
      </span>
    </span>
  );
}

/** Recent database backups as reported by the backup service. */
export function BackupsPanel({ backups }: { backups: Backups }) {
  const last = backups.last_success;
  return (
    <section aria-labelledby="backups-heading" className={cardClass}>
      <h2 id="backups-heading" className="text-lg font-semibold">
        Backups
      </h2>
      <p className="text-sm text-slate-700">
        {`Expected at least every ${String(backups.interval_hours)} hours. `}
        {last
          ? `Last successful backup: ${formatTime(last.finished_at)}${last.size_bytes === null ? "" : `, ${formatBytes(last.size_bytes)}`}${last.encrypted ? ", encrypted" : ""}.`
          : "No successful backup has been reported."}
      </p>
      {last?.recipient && (
        <CopyValue label="Encrypted for (age recipient)" value={last.recipient} />
      )}
      {backups.runs.length === 0 ? (
        <p className="text-sm text-slate-700">The backup service has not reported any runs.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Recent backups</caption>
            <thead className="border-b border-slate-200 text-xs text-slate-600 uppercase">
              <tr>
                <th scope="col" className={cell}>
                  Started
                </th>
                <th scope="col" className={cell}>
                  Duration
                </th>
                <th scope="col" className={cell}>
                  Status
                </th>
                <th scope="col" className={cell}>
                  Size
                </th>
                <th scope="col" className={cell}>
                  SHA-256
                </th>
                <th scope="col" className={cell}>
                  Error
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {backups.runs.map((run) => (
                <tr key={run.id}>
                  <td className={`${cell} whitespace-nowrap`}>{formatTime(run.started_at)}</td>
                  <td className={`${cell} whitespace-nowrap`}>{duration(run)}</td>
                  <td className={cell}>
                    <Badge tone={run.status === "succeeded" ? "good" : "bad"}>
                      {run.status === "succeeded" ? "Succeeded" : "Failed"}
                    </Badge>
                  </td>
                  <td className={`${cell} whitespace-nowrap`}>
                    {run.size_bytes === null ? "" : formatBytes(run.size_bytes)}
                  </td>
                  <td className={cell}>{run.sha256 && <Sha value={run.sha256} />}</td>
                  <td className={`${cell} text-red-900`}>{run.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
