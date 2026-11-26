import { renderMermaidSVG } from "beautiful-mermaid";
import { load } from "cheerio";
import GithubSlugger from "github-slugger";
import MarkdownIt from "markdown-it";
import { parseSrcset, stringifySrcset } from "srcset";

const md = new MarkdownIt({ html: true });
const escape = md.utils.escapeHtml;

md.renderer.rules.fence = (tokens, index) => {
  const { info, content } = tokens[index];
  const language = info.trim().split(/\s+/)[0];
  if (language === "mermaid") {
    const diagram = renderMermaidSVG(content, {
      bg: "var(--page)", fg: "var(--ink)", muted: "var(--secondary)",
      font: "Inter", padding: 16, transparent: true,
    });
    // Use the site's font stack without the renderer's Google Fonts import.
    return '<div class="mermaid-diagram">' + diagram.replace(/^\s*@import url\(.*?\);/m, "") + "</div>\n";
  }

  const label = ["sh", "bash", "shell"].includes(language) ? "Shell" : language || "Code";
  return `<div class="code-example">
  <div class="code-toolbar"><span>${escape(label)}</span>
    <button type="button" class="copy-code" hidden><span class="copy-icon" aria-hidden="true"></span><span class="copy-label">Copy</span></button>
  </div>
  <pre tabindex="0" aria-label="${escape(label)} example"><code>${escape(content)}</code></pre>
</div>\n`;
};

export function renderGuide(markdown, mapUrl) {
  const $ = load(md.render(markdown), {}, false);
  const title = $("h1").first().text();
  if (!title || $("h1").length !== 1) throw new Error("Each guide needs one top-level heading");

  const outline = [];
  const slugger = new GithubSlugger();
  for (const heading of $("h1, h2, h3, h4, h5, h6")) {
    const title = $(heading).text();
    const id = slugger.slug(title);
    const level = Number(heading.tagName.slice(1));
    $(heading).attr("id", id);
    if (level === 2 || level === 3) outline.push({ id, title, level });
  }

  for (const link of $("a[href]")) $(link).attr("href", mapUrl($(link).attr("href")));
  for (const image of $("img[src], source[src]")) $(image).attr("src", mapUrl($(image).attr("src")));
  for (const image of $("img[srcset], source[srcset]")) {
    const sources = parseSrcset($(image).attr("srcset"));
    for (const source of sources) source.url = mapUrl(source.url);
    $(image).attr("srcset", stringifySrcset(sources));
  }

  for (const table of $("table")) $(table).wrap('<div class="table-scroll"></div>');
  for (const element of $(".table-scroll, .mermaid-diagram")) {
    const heading = $(element).prevAll("h1, h2, h3, h4, h5, h6").first();
    const label = heading.text() || title;
    $(element).attr({ role: "region", tabindex: "0", "aria-label": label });
    $(element).children("svg").attr({ role: "img", "aria-label": label });
  }

  return { title, html: $.html(), outline };
}
