import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from 'cheerio';
import MarkdownIt from 'markdown-it';
import { parseSrcset } from 'srcset';

const output = fileURLToPath(new URL('../_site/', import.meta.url));
const guides = ['strategy-study', 'finder', 'ingestion'];
const routes = ['', ...guides.map(slug => `guides/${slug}/`)];
const pages = new Map(routes.map(route => [
  route, load(readFileSync(join(output, route, 'index.html'), 'utf8')),
]));
const base = new URL(pages.get('')('link[rel="canonical"]').attr('href'));
const md = new MarkdownIt({ html: true });

for (const [route, $] of pages) {
  const address = new URL(route, base);
  const attrs = (selector, name) => $(selector).map((_, el) => $(el).attr(name)).get();
  const destinations = selector => attrs(selector, 'href').map(href => new URL(href, address).href);
  const sidebar = destinations('.site-nav a');
  const article = destinations('main article a');
  assert.equal($('link[rel="canonical"]').attr('href'), address.href);
  assert.deepEqual(destinations('.site-nav [aria-current="page"]'), [address.href]);
  for (const other of routes) {
    const url = new URL(other, base).href;
    assert(sidebar.includes(url), `${address}: missing sidebar link to ${url}`);
    if (!route && other) assert(article.includes(url), `Overview needs an article link to ${url}`);
  }

  const references = [
    ...attrs('[href]', 'href'), ...attrs('[src]', 'src'),
    ...attrs('[srcset]', 'srcset').flatMap(value => parseSrcset(value).map(source => source.url)),
  ];
  for (const ref of references) {
    const dest = new URL(ref, address);
    if (dest.origin !== base.origin) continue;
    assert(dest.pathname.startsWith(base.pathname), `${address}: ${ref} escapes the project path`);
    const relative = decodeURIComponent(dest.pathname.slice(base.pathname.length));
    const target = join(output, relative.endsWith('/') || !relative ? relative + 'index.html' : relative);
    assert(existsSync(target) && statSync(target).isFile(), `${address}: missing target ${ref}`);
    if (dest.hash && target.endsWith('.html')) {
      const page = pages.get(relative.replace(/index\.html$/, ''));
      const id = decodeURIComponent(dest.hash.slice(1));
      assert(page && page('[id]').toArray().some(el => page(el).attr('id') === id),
        `${address}: missing fragment ${ref}`);
    }
  }

  const outline = attrs('.page-outline a', 'href');
  assert(outline.length, `${address}: empty outline`);
  if (!route) continue;
  assert.deepEqual(outline, attrs('article h2, article h3', 'id').map(id => '#' + id), `${address}: outline order`);

  // Derive expected elements and cross-guide references from the source on every build.
  const slug = route.split('/')[1];
  const source = new URL(`../results/${slug}/README.md`, import.meta.url);
  const original = load(md.render(readFileSync(source, 'utf8')), {}, false);
  for (const selector of ['table', 'picture', 'picture source[srcset]', 'picture img[src]']) {
    assert.equal($('article').find(selector).length, original(selector).length, `${address}: ${selector}`);
  }
  const blocks = original('pre > code');
  assert.equal($('article .mermaid-diagram svg').length, blocks.filter('.language-mermaid').length);
  const expectedCode = blocks.not('.language-mermaid').map((_, el) => original(el).text()).get();
  assert.deepEqual($('article pre > code').map((_, el) => $(el).text()).get(), expectedCode);
  for (const link of original('a[href]')) {
    const ref = original(link).attr('href');
    const dest = new URL(ref, source);
    const other = guides.find(name => dest.pathname.endsWith(`/results/${name}/README.md`));
    if (dest.protocol === 'file:' && other) {
      const expected = new URL(`guides/${other}/${dest.search}${dest.hash}`, base);
      assert(article.includes(expected.href), `${address}: cross-guide link should stay on site: ${ref}`);
    }
  }
}
console.log(`Checked links, navigation, outlines, and README rendering on ${pages.size} pages.`);
