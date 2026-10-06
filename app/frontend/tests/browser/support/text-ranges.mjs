/* The text range measure the layout certification takes of every rendered
   text range against its own container. It is run inside the page with
   page.evaluate, so it closes over nothing, and it lives here so the layout
   suite and the fixture cases that pin what it catches run the same code. */
/* Text inside a closed details element, at any depth, is laid out but not
   rendered, so its ranges are skipped; a summary is rendered and measured.
   Round three, 24 Sept 2026: text that the element holding it, or an
   ancestor, clips to nothing is not painted either, so it is skipped too.
   The clip is read from the element itself up: overflow other than visible
   on either axis, a clip-path inset and a clip rect, as a visually hidden
   caption or label is set. Overflow on an ancestor is only read where it
   clips the text, so an absolutely or fixed positioned element escapes the
   overflow of the static ancestors between it and its containing block.
   Text whose clip leaves any area is measured as before, against its
   container from the parent up, so a painted range that runs past its
   column is still caught. */
export function measureTextRanges(mark){
  const main = document.querySelector('#main-content');
  const describe = (node) => `${node.tagName.toLowerCase()}${String(node.className || '').trim() ? '.' + String(node.className).trim().split(/\s+/)[0] : ''}`;
  const clipOf = (node) => {
    const style = getComputedStyle(node);
    return {
      any: style.overflowX !== 'visible' || style.overflowY !== 'visible' || node.hasAttribute(mark),
      scrollX: /auto|scroll/.test(style.overflowX),
      scrollY: /auto|scroll/.test(style.overflowY),
    };
  };
  const hiddenByDetails = (element) => {
    let node = element;
    while (node && node !== main){
      if (node.tagName === 'DETAILS' && !node.open){
        const summary = node.querySelector(':scope > summary');
        if (!(summary && summary.contains(element))) return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const lengths = (source, count) => {
    const parts = source.trim().split(/[\s,]+/).filter(Boolean);
    if (!parts.length || parts.length > count || !parts.every((part) => /^-?[\d.]+(px|%)$|^0$|^auto$/.test(part))) return null;
    return parts;
  };
  const measureLength = (part, size, fallback) => part === 'auto' ? fallback : part.endsWith('%') ? size * parseFloat(part) / 100 : parseFloat(part);
  const escapes = (position, style) => {
    const containing = style.position !== 'static' || style.transform !== 'none' || style.filter !== 'none' || /paint|layout|strict|content/.test(style.contain || '');
    if (position === 'absolute') return !containing;
    if (position === 'fixed') return !(style.transform !== 'none' || style.filter !== 'none' || /paint|layout|strict|content/.test(style.contain || ''));
    return false;
  };
  /* The area left painted after every clip from the element up to the main
     region; an empty one means the text never draws. Overflow is not read
     on an inline or display contents box, which it does not clip.
     Round three, 24 Sept 2026: a display contents element makes no box, so
     its rect is empty and a clip-path or clip on it clips nothing; neither
     is read there, and its text is measured as painted. */
  const paintsNothing = (element) => {
    let left = -Infinity, right = Infinity, top = -Infinity, bottom = Infinity;
    let position = null;
    for (let node = element; node && node !== main; node = node.parentElement){
      const style = getComputedStyle(node);
      const box = node.getBoundingClientRect();
      const overflowClips = node === element || !position || !escapes(position, style);
      if (overflowClips && !['inline', 'contents'].includes(style.display)){
        if (style.overflowX !== 'visible'){ left = Math.max(left, box.left); right = Math.min(right, box.right); }
        if (style.overflowY !== 'visible'){ top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom); }
      }
      const boxed = style.display !== 'contents';
      const inset = boxed && /^inset\((.*)\)$/.exec(style.clipPath || '');
      const insets = inset && lengths(inset[1].split(' round ')[0], 4);
      if (insets){
        const [t, r = t, b = t, l = r] = insets;
        left = Math.max(left, box.left + measureLength(l, box.width, 0));
        right = Math.min(right, box.right - measureLength(r, box.width, 0));
        top = Math.max(top, box.top + measureLength(t, box.height, 0));
        bottom = Math.min(bottom, box.bottom - measureLength(b, box.height, 0));
      }
      const rect = boxed && /^rect\((.*)\)$/.exec(style.clip || '');
      const edges = rect && ['absolute', 'fixed'].includes(style.position) && lengths(rect[1], 4);
      if (edges && edges.length === 4){
        const [t, r, b, l] = edges;
        left = Math.max(left, box.left + measureLength(l, box.width, 0));
        right = Math.min(right, box.left + measureLength(r, box.width, box.width));
        top = Math.max(top, box.top + measureLength(t, box.height, 0));
        bottom = Math.min(bottom, box.top + measureLength(b, box.height, box.height));
      }
      if (right - left < 1 || bottom - top < 1) return true;
      if (overflowClips && position && !escapes(position, style)) position = null;
      if (['absolute', 'fixed'].includes(style.position)) position = style.position;
    }
    return false;
  };
  const out = {checked: 0, outside: []};
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  let text;
  while ((text = walker.nextNode())){
    if (!text.textContent.trim()) continue;
    const element = text.parentElement;
    if (!element || ['SCRIPT', 'STYLE'].includes(element.tagName)) continue;
    const style = getComputedStyle(element);
    if (style.visibility === 'hidden' || style.display === 'none' || hiddenByDetails(element)) continue;
    const range = document.createRange();
    range.selectNodeContents(text);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (paintsNothing(element)) continue;
    let container = element.parentElement;
    let clip = null;
    while (container && container !== main){
      const found = clipOf(container);
      if (found.any){ clip = found; break; }
      container = container.parentElement;
    }
    if (!container) container = main;
    if (!clip) clip = {scrollX: false, scrollY: false};
    const box = container.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) continue;
    out.checked += 1;
    const outX = !clip.scrollX && (rect.left < box.left - 1 || rect.right > box.right + 1);
    const outY = !clip.scrollY && (rect.top < box.top - 1 || rect.bottom > box.bottom + 1);
    if (outX || outY){
      out.outside.push(`${describe(element)} "${text.textContent.trim().slice(0, 30)}" range ${[rect.left, rect.right, rect.top, rect.bottom].map(Math.round).join(',')} container ${describe(container)} ${[box.left, box.right, box.top, box.bottom].map(Math.round).join(',')}`);
    }
  }
  return out;
}
