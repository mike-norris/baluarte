import { IconCheck, IconCopy } from "@tabler/icons-react";
import { useState } from "react";

interface DigestDisplayProps {
  value: string;
  label?: string;
}

/** The start and end of a digest, the full value on demand, and a copy button. */
export function DigestDisplay({ value, label = "Digest" }: DigestDisplayProps) {
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const short = value.length > 24 ? `${value.slice(0, 12)}…${value.slice(-6)}` : value;
  const copy = () => {
    if (typeof navigator.clipboard === "undefined") {
      setFull(true);
      return;
    }
    navigator.clipboard.writeText(value).then(
      () => {
        setCopied(true);
      },
      () => {
        setFull(true);
      },
    );
  };
  return (
    <div>
      <div className="d-flex align-items-center gap-2">
        <span className="subheader">{label}</span>
        <button
          type="button"
          className="btn btn-ghost-secondary btn-icon btn-sm"
          aria-label={copied ? `${label} copied` : `Copy ${label.toLowerCase()}`}
          onClick={copy}
        >
          {copied ? <IconCheck size={16} aria-hidden="true" /> : <IconCopy size={16} aria-hidden="true" />}
        </button>
      </div>
      <code className="d-block text-break bg-transparent p-0" title={value}>
        {full ? value : short}
      </code>
      {value !== short && (
        <button
          type="button"
          className="btn btn-link btn-sm px-0"
          aria-expanded={full}
          onClick={() => {
            setFull((shown) => !shown);
          }}
        >
          {full ? "Show less" : "Show full digest"}
        </button>
      )}
      <span className="visually-hidden" role="status">
        {copied ? `${label} copied.` : ""}
      </span>
    </div>
  );
}
