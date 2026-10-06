import {useEffect, useRef} from 'react';
import {EvidenceReadiness} from './EvidenceReadiness.jsx';

export function requestEvidenceClose(onClose, reason, event){
  event?.preventDefault?.();
  onClose?.(reason);
}

export function restoreEvidenceFocus(target){
  if (target?.isConnected && typeof target.focus === 'function') target.focus();
}

export function EvidenceDrawer({
  id = 'evidence-drawer',
  mode = 'region',
  open = true,
  title = 'Evidence',
  readiness = 'unchecked',
  onClose,
  children,
}){
  const isDialog = mode === 'dialog' && typeof onClose === 'function';
  const titleId = `${id}-title`;
  const Element = isDialog ? 'dialog' : 'aside';
  const drawerRef = useRef(null);

  useEffect(() => {
    if (!open || !isDialog) return undefined;
    const dialog = drawerRef.current;
    const previousFocus = document.activeElement;
    if (!dialog?.open) dialog?.showModal();
    return () => {
      if (dialog?.open) dialog.close();
      restoreEvidenceFocus(previousFocus);
    };
  }, [isDialog, open]);

  if (!open) return null;

  function handleKeyDown(event){
    if (isDialog || event.key !== 'Escape' || !onClose) return;
    requestEvidenceClose(onClose, 'keyboard', event);
  }

  return (
    <Element
      className="evidence-drawer"
      data-mode={isDialog ? 'dialog' : 'region'}
      role={isDialog ? 'dialog' : 'region'}
      aria-modal={isDialog ? true : undefined}
      aria-labelledby={titleId}
      ref={drawerRef}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
      onCancel={isDialog ? (event) => requestEvidenceClose(onClose, 'keyboard', event) : undefined}
    >
      <header className="evidence-drawer__head">
        <div className="evidence-drawer__heading">
          <div className="dossier-eyebrow">Evidence</div>
          <h2 className="evidence-drawer__title" id={titleId}>{title}</h2>
          <EvidenceReadiness state={readiness} />
        </div>
        {onClose && (
          <button
            type="button"
            className="evidence-drawer__close"
            aria-label="Close evidence"
            autoFocus={isDialog}
            onClick={() => requestEvidenceClose(onClose, 'button')}
          >
            Close
          </button>
        )}
      </header>
      <div className="evidence-drawer__body">{children}</div>
    </Element>
  );
}
