import { useState, type ReactNode } from "react";

import { linkClass, secondaryClass } from "../components/styles";

/**
 * A small markdown subset for the how-to text the API sends: paragraphs, headings, bullet and
 * numbered lists, fenced code blocks (with a copy button), inline code, bold and links. Text is
 * rendered as React text, never as HTML, and only http(s) and relative links become links.
 */

type Block =
  | { kind: "code"; text: string }
  | { kind: "heading"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "paragraph"; text: string };

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (line.trim().startsWith("```")) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !(lines[index] ?? "").trim().startsWith("```")) {
        code.push(lines[index] ?? "");
        index += 1;
      }
      index += 1;
      blocks.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", text: heading[1] ?? "" });
      index += 1;
      continue;
    }
    const bullet = /^\s*[-*]\s+/;
    const numbered = /^\s*\d+[.)]\s+/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const marker = ordered ? numbered : bullet;
      const items: string[] = [];
      while (index < lines.length && marker.test(lines[index] ?? "")) {
        items.push((lines[index] ?? "").replace(marker, ""));
        index += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    const paragraph: string[] = [];
    while (
      index < lines.length &&
      (lines[index] ?? "").trim() &&
      !(lines[index] ?? "").trim().startsWith("```") &&
      !/^#{1,6}\s/.test(lines[index] ?? "") &&
      !bullet.test(lines[index] ?? "") &&
      !numbered.test(lines[index] ?? "")
    ) {
      paragraph.push((lines[index] ?? "").trim());
      index += 1;
    }
    blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
  }
  return blocks;
}

function safeHref(href: string): string | null {
  if (/^https?:\/\//i.test(href) || href.startsWith("/") || href.startsWith("#")) return href;
  return null;
}

/** Inline code, bold and links; everything else is plain text. */
export function Inline({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const key = `${String(match.index)}`;
    if (match[1] !== undefined) {
      parts.push(
        <code key={key} className="rounded bg-slate-100 px-1 text-[0.9em]">
          {match[1]}
        </code>,
      );
    } else if (match[2] !== undefined) {
      parts.push(<strong key={key}>{match[2]}</strong>);
    } else {
      const href = safeHref(match[4] ?? "");
      parts.push(
        href ? (
          <a key={key} href={href} className={linkClass}>
            {match[3]}
          </a>
        ) : (
          match[3]
        ),
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

export function CodeBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);

  function copy() {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (!clipboard) {
      setCopied("no");
      return;
    }
    clipboard.writeText(code).then(
      () => {
        setCopied("yes");
      },
      () => {
        setCopied("no");
      },
    );
  }

  return (
    <div className="space-y-1">
      <pre className="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
        <code>{code}</code>
      </pre>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={copy}
          aria-label={`Copy ${label}`}
          className={secondaryClass}
        >
          Copy
        </button>
        <span aria-live="polite" className="text-xs text-slate-600">
          {copied === "yes"
            ? "Copied."
            : copied === "no"
              ? "Copy failed; select the text instead."
              : ""}
        </span>
      </div>
    </div>
  );
}

export function Markdown({ source, label }: { source: string; label: string }) {
  const blocks = parseMarkdown(source);
  // Number each code block, so every copy button has its own accessible name.
  const codeNumbers = blocks.map(
    (_, index) => blocks.slice(0, index + 1).filter((block) => block.kind === "code").length,
  );
  return (
    <div className="space-y-3 text-sm text-slate-800">
      {blocks.map((block, index) => {
        const key = String(index);
        switch (block.kind) {
          case "code":
            return (
              <CodeBlock
                key={key}
                code={block.text}
                label={`${label} step ${String(codeNumbers[index] ?? 0)}`}
              />
            );
          case "heading":
            return (
              <p key={key} className="font-semibold">
                <Inline text={block.text} />
              </p>
            );
          case "list": {
            const items = block.items.map((item, itemIndex) => (
              <li key={String(itemIndex)}>
                <Inline text={item} />
              </li>
            ));
            return block.ordered ? (
              <ol key={key} className="list-decimal space-y-1 pl-5">
                {items}
              </ol>
            ) : (
              <ul key={key} className="list-disc space-y-1 pl-5">
                {items}
              </ul>
            );
          }
          default:
            return (
              <p key={key}>
                <Inline text={block.text} />
              </p>
            );
        }
      })}
    </div>
  );
}
