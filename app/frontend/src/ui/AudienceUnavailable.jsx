/* The audience refusal, stated rather than filled in.

   42 will not infer age or gender from platform behaviour, so when no approved
   measurement is connected the surface says exactly that and stops. It moved
   here from the retired Intelligence board because the refusal outlives the
   board: any surface that could otherwise imply a demographic reads it. */

const REASONS = {
  approved_measurement_not_connected:
    '42 will not infer age or gender from platform behaviour. A measured audience read needs its source, window, sample, method and confidence before any demographic label or percentage can appear.',
};

export function AudienceUnavailable({audience, marketLabel}){
  /* Any unavailable reason renders. The component previously returned null for
     every reason but one, so an unsupported audience claim met silence, and
     silence reads as a question that was answered. */
  if (audience?.state !== 'unavailable') return null;
  const body = REASONS[audience?.reason]
    || 'No approved audience measurement is connected for this read, so no demographic label can be stated.';
  return (
    <section className="audience-unavailable" role="status" aria-label="Audience evidence">
      <p className="audience-unavailable__eyebrow">
        Audience evidence · {marketLabel} · measurement unavailable
      </p>
      <h2 className="audience-unavailable__title">Audience measurement not connected</h2>
      <p className="audience-unavailable__body">{body}</p>
    </section>
  );
}

export default AudienceUnavailable;
