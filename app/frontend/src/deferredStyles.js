/* Both ends of the deferred window are written to the document's performance
   timeline, so the time a route surface can spend on screen before its
   stylesheet lands is a number a test can read, not something assumed away.
   product-styles:loading is the first frame after first paint, when the
   deferral begins; product-styles:ready is the moment the deferred half has
   applied and the document has been told so; product-styles:failed is
   written instead when the load never lands or the ready callback throws,
   so the marks agree with the attribute the page carries. */
function performanceMark(name){
  if (typeof performance !== 'undefined' && typeof performance.mark === 'function') performance.mark(name);
}

export function scheduleDeferredStyles({
  frame,
  timer = setTimeout,
  criticalStylesReady = () => true,
  load,
  onReady = () => {},
  onFailure,
  retryDelay = 250,
  mark = performanceMark,
}){
  const run = (retriesRemaining) => Promise.resolve()
    .then(() => {
      if (!criticalStylesReady()) throw new Error('Critical package stylesheet unavailable');
      return load();
    })
    .then(
      () => {
        try {
          onReady();
        } catch (error) {
          mark('product-styles:failed');
          onFailure(error);
          return;
        }
        mark('product-styles:ready');
      },
      (error) => {
        if (retriesRemaining > 0){
          timer(() => run(retriesRemaining - 1), retryDelay);
          return;
        }
        mark('product-styles:failed');
        onFailure(error);
      },
    );
  const begin = () => {
    mark('product-styles:loading');
    return run(1);
  };
  if (typeof frame === 'function') frame(begin);
  else timer(begin, 0);
}
