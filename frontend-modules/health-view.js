// Health view boundary: pure formatting/normalisation helpers shared by the SPA.
// The monolith keeps a fallback so an older cached page can still boot offline.
export const DEFAULT_HEALTH_DAYS = 7;
export const HEALTH_RANGES = Object.freeze([7, 14, 30]);

export function healthDays(value) {
  const n = Number(value);
  return HEALTH_RANGES.includes(n) ? n : DEFAULT_HEALTH_DAYS;
}

export function healthIngestLabel(ingest) {
  const status = ingest && ingest.status;
  if (status === 'ok') return '接收正常';
  if (status === 'stale') return `接收陈旧${ingest.age_hours != null ? `（${ingest.age_hours}h）` : ''}`;
  if (status === 'empty') return '尚未收到原始数据';
  return '';
}

export function healthCoverage(data) {
  const days = Array.isArray(data && data.days) ? data.days.length : 0;
  const fields = ['sleep', 'heart', 'energy', 'exercise', 'hrv', 'rhr', 'spo2', 'resp', 'steps', 'dist'];
  const missing = [];
  const partial = [];
  fields.forEach((key) => {
    const count = Array.isArray(data && data[key]) ? data[key].length : 0;
    if (!count) missing.push(key);
    else if (days && count < days) partial.push({key, count, days});
  });
  return {days, missing, partial};
}
