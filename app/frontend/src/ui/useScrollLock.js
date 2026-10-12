/* Holds the page still behind an open dialog. While the dialog is mounted the
   html and body elements both take overflow hidden, since either one can be
   the scroller depending on the page's own styles; when it closes each gets
   back exactly the overflow it had before it opened. */
import {useEffect} from 'react';

export function useScrollLock(){
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const page = [document.documentElement, document.body];
    const before = page.map((node) => node.style.overflow);
    page.forEach((node) => { node.style.overflow = 'hidden'; });
    return () => page.forEach((node, index) => { node.style.overflow = before[index]; });
  }, []);
}
