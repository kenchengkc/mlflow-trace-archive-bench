import { copyFileSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from 'cheerio';
import { renderGuide } from './render-guide.mjs';

const docs = path.dirname(fileURLToPath(import.meta.url));
const repo = path.dirname(docs);
const output = path.join(repo, '_site');
const repoUrl = 'https://github.com/kenchengkc/mlflow-trace-archive-bench';
const siteUrl = 'https://kenchengkc.github.io/mlflow-trace-archive-bench/';
const template = readFileSync(path.join(docs, 'layout.html'), 'utf8');
const guideRoutes = new Map(['strategy-study', 'finder', 'ingestion'].map(slug => [
  `results/${slug}/README.md`, `guides/${slug}/`,
]));

function publishFile(source) {
  const dest = path.join(output, source);
  mkdirSync(path.dirname(dest), { recursive: true });
  copyFileSync(path.join(repo, source), dest);
}

// Resolve Markdown links against the README's repository location, then relocate
// them for the published page. Code and raw results keep their GitHub URLs.
function guideLink(source, value) {
  if (!value || value.startsWith('#') || value.startsWith('?') || /^(?:[a-z][\w+.-]*:|\/\/)/i.test(value)) {
    return value;
  }
  const url = new URL(value, `https://repo.invalid/${source}`);
  const target = decodeURIComponent(url.pathname.slice(1));
  const suffix = url.search + url.hash;
  if (guideRoutes.has(target)) return '../../' + guideRoutes.get(target) + suffix;
  if (target === 'README.md') {
    if (url.hash === '#reproduce-the-benchmark') return '../../' + url.search + '#local-benchmark';
    if (!url.hash || url.hash === '#quick-start') return '../../' + suffix;
  }
  const info = statSync(path.join(repo, target)); // Fail the build on a broken repository link.
  if (/\.(?:png|jpeg|jpg|gif|svg|webp)$/i.test(target)) {
    publishFile(target);
    return '../../' + url.pathname.slice(1) + suffix;
  }
  return repoUrl + (info.isDirectory() ? '/tree/main' : '/blob/main') + url.pathname + suffix;
}

function writePage(route, content, outline, source) {
  const root = route ? '../../' : './';
  const $ = load(template.replaceAll('{{root}}', root));
  const article = $('main article').html(content);
  const currentHref = root + route;
  $('.site-nav a').filter((_, a) => $(a).attr('href') === currentHref).attr('aria-current', 'page');
  for (const { title, level, id } of outline) {
    const li = $('<li>').toggleClass('outline-subheading', level > 2);
    li.append($('<a>').attr('href', '#' + id).text(title));
    $('.page-outline ul').append(li);
  }

  if (source) {
    article.addClass('guide');
    const title = article.find('h1').first().text();
    const intro = article.children('p').clone();
    intro.find('a').remove();
    const description = intro.toArray().map(p => $(p).text().trim()).find(Boolean);
    $('title').text(`${title} | MLflow Trace Archival Benchmark`);
    $('meta[property="og:title"]').attr('content', title);
    $('meta[name="description"], meta[property="og:description"]').attr('content', description || title);
    $('meta[property="og:type"]').attr('content', 'article');
    $('meta[property^="og:image"]').remove();
    $('meta[name="twitter:card"]').attr('content', 'summary');
    $('footer nav').prepend($('<a>').attr('href', `${repoUrl}/blob/main/${source}`).text('View README on GitHub'));
  }
  $('link[rel="canonical"]').attr('href', siteUrl + route);
  $('meta[property="og:url"]').attr('content', siteUrl + route);
  const dest = path.join(output, route);
  mkdirSync(dest, { recursive: true });
  writeFileSync(path.join(dest, 'index.html'), $.html());
}

mkdirSync(output, { recursive: true });
for (const asset of ['style.css', 'ui.js', 'llms.txt']) {
  copyFileSync(path.join(docs, asset), path.join(output, asset));
}
const overview = readFileSync(path.join(docs, 'overview.html'), 'utf8');
const $ = load(overview, {}, false);
const outline = $('section[id]').map((_, section) => ({
  id: $(section).attr('id'), title: $(section).children('h2').text(), level: 2,
})).get();
for (const image of $('picture [src], picture [srcset]').toArray()) {
  publishFile($(image).attr('src') ?? $(image).attr('srcset'));
}
writePage('', overview, outline);
for (const [source, route] of guideRoutes) {
  const markdown = readFileSync(path.join(repo, source), 'utf8');
  const guide = renderGuide(markdown, url => guideLink(source, url));
  writePage(route, guide.html, guide.outline, source);
}
