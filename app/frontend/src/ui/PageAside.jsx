/* PULSE ui · PageAside: a secondary column beside the main content, for
   filters, meta, or a rail. Sticks below the masthead on wide viewports and
   drops inline on narrow ones (handled in ui.css). */

export function PageAside({children, className}){
  return (
    <aside className={'ui-page-aside' + (className ? ' ' + className : '')}>
      {children}
    </aside>
  );
}
