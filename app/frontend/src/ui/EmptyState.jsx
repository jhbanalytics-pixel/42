/* PULSE ui · EmptyState: delegates to the shared desk EmptyState in parts.jsx
   so error and loader homes share one primitive. cta maps to a primary action. */

import { EmptyState as DeskEmptyState } from '../parts.jsx';

/* isLoading travels through. Without it every loader rendered frozen from the
   public API, so a surface that was genuinely still fetching looked settled. */
export function EmptyState({title, body, cta, loader, field, status, actions, isLoading = false}){
  const acts = (actions || []).filter(Boolean);
  if (!acts.length && cta && cta.label) {
    acts.push({label: cta.label, onClick: cta.onClick, primary: true});
  }
  return (
    <DeskEmptyState
      loader={loader === undefined ? false : loader}
      isLoading={isLoading}
      field={field}
      title={title}
      body={body}
      status={status || []}
      actions={acts}
    />
  );
}
