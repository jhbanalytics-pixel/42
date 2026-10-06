function cloneValue(value, active){
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return value;
  if (typeof value !== 'object' || active.has(value)) throw new TypeError('plain_data_required');

  const prototype = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null){
    throw new TypeError('plain_data_required');
  }

  const keys = Reflect.ownKeys(value);
  if (keys.some((key) => typeof key === 'symbol')) throw new TypeError('plain_data_required');
  const descriptors = Object.getOwnPropertyDescriptors(value);
  if (Object.values(descriptors).some((descriptor) => !Object.hasOwn(descriptor, 'value'))){
    throw new TypeError('plain_data_required');
  }

  active.add(value);
  const array = Array.isArray(value);
  const dataKeys = array ? keys.filter((key) => key !== 'length') : keys;
  if (array && (dataKeys.length !== value.length
    || dataKeys.some((key, index) => key !== String(index)))){
    throw new TypeError('plain_data_required');
  }
  const clone = array ? [] : Object.create(prototype);
  for (const key of dataKeys){
    const descriptor = descriptors[key];
    if (!descriptor?.enumerable) throw new TypeError('plain_data_required');
    Object.defineProperty(clone, key, {
      value: cloneValue(descriptor.value, active),
      enumerable: true,
      writable: true,
      configurable: true,
    });
  }
  active.delete(value);
  return clone;
}

export function safePlainData(value){
  try {
    const clone = cloneValue(value, new WeakSet());
    if (value && typeof value === 'object') structuredClone(value);
    return {ok: true, value: clone};
  } catch {
    return {ok: false, value: null};
  }
}
