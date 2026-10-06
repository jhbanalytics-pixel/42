/* PULSE ui · PageShell: the max-width column wrapper duplicated inline across
   routes (e.g. creator.jsx's maxWidth: var(--maxw) block). One place for the
   page gutter and column cap. accent scopes a lane color locally via
   data-accent (see the lane table in v3-design-system.md); it never sets a
   global preference, so the theme picker still wins as a manual override. */

export function PageShell({children, accent, className}){
  return (
    <div
      className={'ui-page-shell' + (className ? ' ' + className : '')}
      data-accent={accent || undefined}
    >
      {children}
    </div>
  );
}
