import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown, splitFrontMatter } from "./markdown";
import type { Job } from "./types";
import { dur, exitText, jobTitle, progressPct, size } from "./util";

const html = (src: string, opts = {}) => renderToStaticMarkup(<Markdown src={src} {...opts} />);
const job = (over: Partial<Job>): Job => ({
  id: "abc", repo: "0:x", repo_name: "x", action: "generate", options: {}, status: "running", dry_run: false,
  exit_code: null, created: 0, started: 1, ended: null, seconds: 3, stage: "", downloads: [], result: null,
  position: null, ...over,
});

describe("markdown for generated docs", () => {
  it("renders headings, fenced code, tables and lists", () => {
    const h = html("---\ntitle: X\n---\n# Nginx Panel\n\n## Install\n\n1. Start it:\n\n   ```bash\n   systemctl start x\n   ```\n2. Done\n\n| a | b |\n|---|--:|\n| x | 1 |\n");
    expect(h).toContain('<h1 id="nginx-panel">Nginx Panel</h1>');
    expect(h).not.toContain("title: X");
    expect(html("<!-- generated-by: docpipe -->\n# T")).toBe('<h1 id="t">T</h1>');
    expect(h).toContain("<ol>");
    expect(h).toContain('data-lang="bash"');
    expect(h).toContain("systemctl start x");
    expect(h).toContain('<td style="text-align:right">1</td>');
  });
  it("renders admonitions, nested lists, italics and links safely", () => {
    const h = html('!!! warning "Careful"\n    Back up first.\n\n- a\n  - b\n\n*soft* and [site](https://example.org) and [bad](javascript:alert(1))');
    expect(h).toContain("md-admonition warning");
    expect(h).toContain("Careful");
    expect(h).toMatch(/<ul><li>a<ul><li>b<\/li><\/ul><\/li><\/ul>/);
    expect(h).toContain("<em>soft</em>");
    expect(h).toContain('href="https://example.org"');
    expect(h).not.toMatch(/href="javascript/i);
    expect(h).toContain('class="md-deadlink"');
  });
  it("never turns input into HTML and leaves underscores alone", () => {
    const h = html('<img src=x onerror=alert(1)> replica_count and `<script>`');
    expect(h).not.toContain("<img");
    expect(h).not.toContain("<script>");
    expect(h).toContain("replica_count");
  });
  it("resolves links to other pages", () => {
    const h = html("[role](haproxy.md)", { resolve: (x: string) => (x === "haproxy.md" ? "nl/p/haproxy.md" : null), onNavigate: () => {} });
    expect(h).toContain('href="#"');
    expect(splitFrontMatter("---\nrevision: abc\n---\nbody")[0].revision).toBe("abc");
  });
});

describe("jobs", () => {
  it("describes generate jobs", () => {
    expect(jobTitle(job({}))).toBe("Generate docs");
    expect(jobTitle(job({ dry_run: true }))).toBe("Dry run");
    expect(exitText(job({ stage: "Translating owncast (NL->EN)" }))).toBe("Translating owncast (NL->EN)");
    expect(exitText(job({ status: "queued", position: 2 }))).toBe("waiting · #2 in the queue");
    expect(exitText(job({ status: "done", result: { status: "ok", issues: 2, pages: 4, error: null } }))).toBe("4 pages · 2 QA warnings");
    expect(progressPct(job({}))).toBeNull();
    expect(dur(75)).toBe("1m 15s");
    expect(size(2048)).toBe("2 KB");
  });
});
