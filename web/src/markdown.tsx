import type { ReactNode } from "react";

// A safe Markdown renderer for the generated pages. Everything becomes React elements and
// text nodes, never HTML: a page cannot inject markup or scripts into the UI.
//
// Supported: front matter (skipped), headings, paragraphs, fenced code, tables, ordered and
// unordered (nested) lists, blockquotes, MkDocs admonitions (!!! note "Title"), rules,
// `code`, **bold**, *italic*, [links](...) and ![images](...) (shown as their alt text).
// No _underscore emphasis_: names such as replica_count must stay intact.

export interface MdOptions {
  /** Turn a relative link (other page) into a page path the viewer can open, or null. */
  resolve?: (href: string) => string | null;
  onNavigate?: (path: string) => void;
}

let keySeq = 0;
const k = () => keySeq++;

function safeHref(href: string): string | null {
  const h = href.trim();
  if (/^(https?:|mailto:)/i.test(h)) return h;
  if (h.startsWith("#")) return h;
  return null;
}

export function inline(text: string, opts: MdOptions = {}): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|!\[[^\]]*\]\([^)]*\)|\[[^\]]+\]\([^)\s]+(?:\s+"[^"]*")?\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("`")) out.push(<code key={k()}>{t.slice(1, -1)}</code>);
    else if (t.startsWith("**")) out.push(<strong key={k()}>{inline(t.slice(2, -2), opts)}</strong>);
    else if (t.startsWith("![")) {
      const alt = t.slice(2, t.indexOf("]"));
      out.push(<span key={k()} className="md-img" title="image (not loaded)">🖼 {alt || "image"}</span>);
    } else if (t.startsWith("[")) {
      const label = t.slice(1, t.indexOf("]("));
      const href = t.slice(t.indexOf("](") + 2, -1).replace(/\s+"[^"]*"$/, "");
      const page = opts.resolve ? opts.resolve(href) : null;
      const safe = safeHref(href);
      if (page && opts.onNavigate) {
        out.push(<a key={k()} href="#" onClick={(e) => { e.preventDefault(); opts.onNavigate!(page); }}>{inline(label, opts)}</a>);
      } else if (safe) {
        out.push(<a key={k()} href={safe} target={safe.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer">{inline(label, opts)}</a>);
      } else {
        out.push(<span key={k()} className="md-deadlink" title={href}>{inline(label, opts)}</span>);
      }
    } else out.push(<em key={k()}>{inline(t.slice(1, -1), opts)}</em>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function slugify(text: string): string {
  return text.toLowerCase().replace(/[`*[\]()]/g, "").trim().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "");
}

const indentOf = (l: string) => l.match(/^ */)![0].length;
const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function blocks(lines: string[], opts: MdOptions): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }

    const fence = l.match(/^\s*(```+|~~~+)\s*([\w+-]*)/);
    if (fence) {
      const close = fence[1];
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      out.push(
        <pre key={k()} className="md-code" data-lang={fence[2] || undefined}>
          {fence[2] && <span className="md-lang">{fence[2]}</span>}
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const h = l.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) {
      const level = h[1].length;
      const H = `h${level}` as "h1";
      out.push(<H key={k()} id={slugify(h[2])}>{inline(h[2], opts)}</H>);
      i++;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push(<hr key={k()} />); i++; continue; }

    const adm = l.match(/^(!!!|\?\?\?\+?)\s+(\w+)(?:\s+"([^"]*)")?\s*$/);
    if (adm) {
      const body: string[] = [];
      i++;
      while (i < lines.length && (!lines[i].trim() || indentOf(lines[i]) >= 4)) body.push(lines[i++].slice(4));
      const kind = adm[2].toLowerCase();
      out.push(
        <div key={k()} className={`md-admonition ${kind}`}>
          <div className="md-admonition-title">{adm[3] ?? kind.charAt(0).toUpperCase() + kind.slice(1)}</div>
          {blocks(body, opts)}
        </div>,
      );
      continue;
    }

    if (l.trimStart().startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith(">")) body.push(lines[i++].trimStart().replace(/^>\s?/, ""));
      out.push(<blockquote key={k()}>{blocks(body, opts)}</blockquote>);
      continue;
    }

    if (l.trimStart().startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith("|")) rows.push(lines[i++].trim());
      const cells = (r: string) => r.replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
      const hasAlign = rows[1] !== undefined && /^\|?\s*:?-+/.test(rows[1]);
      const align = hasAlign ? cells(rows[1]).map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : undefined)) : [];
      const body = rows.slice(hasAlign ? 2 : 1);
      out.push(
        <div key={k()} className="md-table">
          <table>
            <thead><tr>{cells(rows[0]).map((c, j) => <th key={j} style={align[j] ? { textAlign: align[j] } : undefined}>{inline(c, opts)}</th>)}</tr></thead>
            <tbody>{body.map((r, n) => <tr key={n}>{cells(r).map((c, j) => <td key={j} style={align[j] ? { textAlign: align[j] } : undefined}>{inline(c, opts)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }

    const li = l.match(LIST_RE);
    if (li) {
      const base = li[1].length;
      const ordered = /\d/.test(li[2]);
      const items: string[][] = [];
      while (i < lines.length) {
        const cur = lines[i];
        const m = cur.match(LIST_RE);
        if (m && m[1].length === base && /\d/.test(m[2]) === ordered) {
          items.push([m[3]]);
          i++;
          continue;
        }
        // continuation / nested content: indented deeper, or a blank line followed by such
        if (cur.trim() && indentOf(cur) > base) { items[items.length - 1].push(cur.slice(Math.min(indentOf(cur), base + 4))); i++; continue; }
        if (!cur.trim() && i + 1 < lines.length && lines[i + 1].trim() && indentOf(lines[i + 1]) > base) { items[items.length - 1].push(""); i++; continue; }
        break;
      }
      const start = ordered ? parseInt(li[2], 10) : undefined;
      const render = (it: string[]) => {
        const [first, ...rest] = it;
        const restText = rest.join("\n").trim();
        return <>{inline(first, opts)}{restText && blocks(rest, opts)}</>;
      };
      out.push(ordered
        ? <ol key={k()} start={start !== 1 ? start : undefined}>{items.map((it, n) => <li key={n}>{render(it)}</li>)}</ol>
        : <ul key={k()}>{items.map((it, n) => <li key={n}>{render(it)}</li>)}</ul>);
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(\s*(```|~~~)|#{1,6}\s|\s*\||\s*>|!!!|\s*([-*+]|\d+[.)])\s)/.test(lines[i])) para.push(lines[i++].trim());
    if (!para.length) { para.push(lines[i++].trim()); }
    out.push(<p key={k()}>{inline(para.join(" "), opts)}</p>);
  }
  return out;
}

/** Strip YAML front matter; returns [front matter fields, body]. */
export function splitFrontMatter(src: string): [Record<string, string>, string] {
  const m = src.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m) return [{}, src];
  const fields: Record<string, string> = {};
  for (const line of m[1].split("\n")) {
    const f = line.match(/^([\w-]+):\s*["']?(.*?)["']?\s*$/);
    if (f) fields[f[1]] = f[2];
  }
  return [fields, src.slice(m[0].length)];
}

export function Markdown({ src, ...opts }: { src: string } & MdOptions) {
  const [, body] = splitFrontMatter(src);
  // HTML comments (e.g. docpipe's "generated-by" marker) are not shown, as in MkDocs.
  const text = body.replace(/\r\n/g, "\n").replace(/<!--[\s\S]*?-->/g, "");
  return <>{blocks(text.split("\n"), opts)}</>;
}
