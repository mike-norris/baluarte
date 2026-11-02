/**
 * A reader for the YAML GitLab returns for one security policy, used only to summarise it.
 *
 * GitLab writes block-style YAML: mappings, `- ` sequences (also level with their key), plain,
 * single- and double-quoted scalars that may wrap onto more-indented lines, and `[]` or `{}`.
 * Anything else (anchors, tags, block scalars, flow collections with content) makes the reader
 * give up and return undefined, and the page shows only the verbatim YAML. The summary is a
 * reading aid; the YAML is what GitLab enforces.
 */

export type YamlValue =
  string | number | boolean | null | YamlValue[] | { [key: string]: YamlValue };

class Unsupported extends Error {}

interface Line {
  indent: number;
  text: string;
}

const KEY = /^([A-Za-z0-9_][A-Za-z0-9_ .-]*):(?:[ \t]+(.*))?$/;

function lines(source: string): Line[] {
  const out: Line[] = [];
  for (const raw of source.replace(/\r\n?/g, "\n").split("\n")) {
    if (raw.includes("\t")) throw new Unsupported("tab indentation");
    const text = raw.trim();
    if (text === "" || text.startsWith("#") || text === "---" || text === "...") continue;
    out.push({ indent: raw.length - raw.trimStart().length, text: raw.trim() });
  }
  return out;
}

function isItem(text: string): boolean {
  return text === "-" || text.startsWith("- ");
}

class Reader {
  private pos = 0;

  constructor(private readonly input: Line[]) {}

  read(): YamlValue {
    if (this.input.length === 0) return null;
    const first = this.input[0];
    if (!first || first.indent !== 0) throw new Unsupported("document must start at column 0");
    const value = this.node(0);
    if (this.pos !== this.input.length) throw new Unsupported("unexpected indentation");
    return value;
  }

  private peek(): Line | undefined {
    return this.input[this.pos];
  }

  private node(indent: number): YamlValue {
    const line = this.peek();
    if (!line) return null;
    return isItem(line.text) ? this.sequence(indent) : this.mapping(indent);
  }

  private mapping(indent: number): { [key: string]: YamlValue } {
    const out: { [key: string]: YamlValue } = {};
    for (let line = this.peek(); line && line.indent === indent; line = this.peek()) {
      if (isItem(line.text)) break;
      const match = KEY.exec(line.text);
      if (!match) throw new Unsupported("not a key");
      const key = match[1] ?? "";
      const rest = match[2];
      this.pos += 1;
      if (Object.hasOwn(out, key)) throw new Unsupported("duplicate key");
      if (rest === undefined || rest === "") {
        const next = this.peek();
        if (next && next.indent > indent) out[key] = this.node(next.indent);
        else if (next && next.indent === indent && isItem(next.text)) {
          out[key] = this.sequence(indent);
        } else out[key] = null;
      } else {
        out[key] = this.scalar(rest, indent);
      }
    }
    const after = this.peek();
    if (after && after.indent > indent) throw new Unsupported("unexpected indentation");
    return out;
  }

  private sequence(indent: number): YamlValue[] {
    const out: YamlValue[] = [];
    for (let line = this.peek(); line && line.indent === indent; line = this.peek()) {
      if (!isItem(line.text)) break;
      const rest = line.text.slice(1).trimStart();
      if (rest === "") {
        this.pos += 1;
        const next = this.peek();
        out.push(next && next.indent > indent ? this.node(next.indent) : null);
      } else if (KEY.test(rest) && !/^["']/.test(rest)) {
        // "- key: value" opens a mapping whose keys sit two columns in.
        const inner = indent + (line.text.length - rest.length);
        this.input[this.pos] = { indent: inner, text: rest };
        out.push(this.mapping(inner));
      } else {
        this.pos += 1;
        out.push(this.scalar(rest, indent));
      }
    }
    return out;
  }

  /** A scalar on its owner's line, joined with any more-indented continuation lines. */
  private scalar(first: string, ownerIndent: number): YamlValue {
    let text = first;
    for (let next = this.peek(); next && next.indent > ownerIndent; next = this.peek()) {
      text += ` ${next.text}`;
      this.pos += 1;
    }
    return interpret(text);
  }
}

function interpret(raw: string): YamlValue {
  const text = raw.trim();
  if (/^[&*!|>]/.test(text) || text.startsWith("{") || text.startsWith("[")) {
    if (text === "[]") return [];
    if (text === "{}") return {};
    throw new Unsupported("anchors, tags, block scalars and flow collections");
  }
  if (text.startsWith("'")) {
    if (text.length < 2 || !text.endsWith("'")) throw new Unsupported("unterminated quote");
    return text.slice(1, -1).replace(/''/g, "'");
  }
  if (text.startsWith('"')) {
    try {
      return JSON.parse(text) as string;
    } catch {
      throw new Unsupported("double-quoted escape");
    }
  }
  const plain = text.replace(/\s+#.*$/, "");
  if (plain === "true") return true;
  if (plain === "false") return false;
  if (plain === "null" || plain === "~") return null;
  if (/^-?\d+(\.\d+)?$/.test(plain)) return Number(plain);
  return plain;
}

/** The parsed policy, or undefined when it uses YAML this reader does not handle. */
export function readPolicyYaml(source: string | null): YamlValue | undefined {
  if (!source) return undefined;
  try {
    return new Reader(lines(source)).read();
  } catch (error) {
    if (error instanceof Unsupported) return undefined;
    throw error;
  }
}
