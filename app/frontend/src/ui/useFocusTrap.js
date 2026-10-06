/* Keeps Tab inside a confirm. Tab on the last control wraps to the first and
   Shift+Tab on the first wraps to the last; Tab between them is left to the
   browser. It sets no first focus and returns none, so each dialog keeps its
   own: Cancel first, and focus back to the opener on close. The stops are
   the ones a browser tabs through: hidden controls are skipped, and a radio
   group is one stop, its checked radio or else its first. With no stop at
   all, Tab is left alone. */
import {useEffect} from 'react';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// happy-dom gives every element one client rect, so checkVisibility and the
// hidden attribute do the work there; getClientRects covers older browsers.
const shown = (node) => !node.closest('[hidden]')
  && (typeof node.checkVisibility === 'function' ? node.checkVisibility() : node.getClientRects().length > 0);

function stops(el){
  const all = [...el.querySelectorAll(FOCUSABLE)].filter(shown);
  return all.filter((node) => {
    if (node.type !== 'radio' || !node.name) return true;
    const group = all.filter((r) => r.type === 'radio' && r.name === node.name && r.form === node.form);
    const checked = group.find((r) => r.checked);
    return node === (checked || group[0]);
  });
}

export function useFocusTrap(box){
  useEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    const onKey = (e) => {
      if (e.key !== 'Tab') return;
      const all = stops(el);
      if (!all.length) return;
      const first = all[0];
      const last = all[all.length - 1];
      if (e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
    };
    el.addEventListener('keydown', onKey);
    return () => el.removeEventListener('keydown', onKey);
  }, [box]);
}
