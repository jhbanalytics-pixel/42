/* A saved file's blob URL is released a minute after the click, because some
   browsers lose the file if it goes before the download has begun. A download
   that fails before the click releases it at once (the caller does that). */
export const EXPORT_URL_LIFETIME_MS = 60000;

export function releaseExportUrlLater(url){
  setTimeout(() => URL.revokeObjectURL(url), EXPORT_URL_LIFETIME_MS);
}
