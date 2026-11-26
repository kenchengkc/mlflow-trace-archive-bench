if (navigator.clipboard?.writeText) {
  for (const example of document.querySelectorAll(".code-example")) {
    const button = example.querySelector("button.copy-code");
    const label = button.querySelector(".copy-label");
    const code = example.querySelector("pre code");
    const initialLabel = label.textContent;
    let reset;

    label.setAttribute("aria-live", "polite");
    button.hidden = false;
    button.addEventListener("click", async () => {
      clearTimeout(reset);
      try {
        await navigator.clipboard.writeText(code.textContent);
        label.textContent = "Copied";
      } catch {
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        label.textContent = "Copy manually";
      }
      reset = setTimeout(() => { label.textContent = initialLabel; }, 2500);
    });
  }
}

const menu = document.querySelector("details.site-menu");
const compact = matchMedia("(max-width: 52rem)");
if (menu) {
  const pageOutline = document.querySelector("nav.page-outline");
  const menuBody = menu.querySelector(".menu-body");
  if (pageOutline && menuBody) {
    const mergedColumns = matchMedia("(max-width: 70rem)");
    const originalPosition = document.createComment("Page outline");
    pageOutline.before(originalPosition);
    const placeOutline = () => {
      const focused = document.activeElement;
      const restoreFocus = pageOutline.contains(focused);
      if (mergedColumns.matches) menuBody.prepend(pageOutline);
      else originalPosition.after(pageOutline);
      if (restoreFocus) {
        const focusTarget = compact.matches && !menu.open ? menu.querySelector("summary") : focused;
        focusTarget.focus({ preventScroll: true });
      }
    };
    placeOutline();
    mergedColumns.addEventListener("change", placeOutline);
  }
  const sizeMenu = () => {
    const focusInMenu = menu.contains(document.activeElement);
    menu.open = !compact.matches;
    if (!menu.open && focusInMenu) menu.querySelector("summary").focus({ preventScroll: true });
  };
  sizeMenu();
  compact.addEventListener("change", sizeMenu);
  menu.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!compact.matches || !link || event.defaultPrevented || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    if (link.origin !== location.origin || link.pathname !== location.pathname ||
        link.search !== location.search) return;
    const target = document.getElementById(link.hash.slice(1));
    if (!target) return;

    menu.open = false;
    if (!target.hasAttribute("tabindex")) target.tabIndex = -1;
    target.focus({ preventScroll: true });
  });
}

const outline = [...document.querySelectorAll('nav.page-outline a[href^="#"]')].map(link => ({
  link,
  section: document.getElementById(link.hash.slice(1)),
})).filter(entry => entry.section);

if (outline.length) {
  function updateOutline() {
    const readingLine = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) + 8;
    const docHeight = document.documentElement.scrollHeight;
    const atEnd = window.scrollY > 0 && window.scrollY + innerHeight >= docHeight - 2;
    const current = atEnd ? outline[outline.length - 1] :
      outline.filter(entry => entry.section.getBoundingClientRect().top <= readingLine).pop();
    for (const entry of outline) {
      if (entry === current) entry.link.setAttribute("aria-current", "location");
      else entry.link.removeAttribute("aria-current");
    }
  }
  updateOutline();
  window.addEventListener("scroll", updateOutline, { passive: true });
  window.addEventListener("resize", updateOutline);
  window.addEventListener("pageshow", updateOutline);
}
