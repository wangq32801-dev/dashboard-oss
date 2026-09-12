// Pure helpers for the Today surface. DOM and application state remain in the
// current bundle until the view is fully migrated.
export function greetingForHour(hour) {
  const h = Number.isFinite(Number(hour)) ? Number(hour) : 12;
  if (h < 6) return ['🌙', '夜深了'];
  if (h < 11) return ['☀️', '早安'];
  if (h < 14) return ['🌤', '中午好'];
  if (h < 18) return ['⛅', '下午好'];
  return ['🌆', '晚上好'];
}

export function completionPercent(items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return 0;
  return Math.round(list.filter(item => item && item.checked).length / list.length * 100);
}

function dueDay(task) {
  const value = String(task?.due || '').slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '';
}

export function isWeeklyRock(task, rockIds = []) {
  const ids = rockIds instanceof Set ? rockIds : new Set((rockIds || []).map(String));
  const tagged = (task?.tags || []).some(tag => String(tag).includes('大石头'));
  return tagged || ids.has(String(task?.id || ''));
}

// Today is an execution queue, not a mirror of every future task. Weekly rocks
// stay visible regardless of due date; otherwise only overdue, due-today, and
// undated next actions are eligible. Priority affects order, never rock identity.
export function selectTodayFocusTasks(items, {today, rockIds = []} = {}) {
  const day = String(today || '').slice(0, 10);
  const ids = rockIds instanceof Set ? rockIds : new Set((rockIds || []).map(String));
  const rank = task => {
    if (isWeeklyRock(task, ids)) return 0;
    const due = dueDay(task);
    if (due && day && due < day) return 1;
    if (due && day && due === day) return 2;
    if ((task?.quad || 'q2') === 'q2') return 3;
    if ((task?.quad || '') === 'q1') return 4;
    return 5;
  };
  const eligible = (Array.isArray(items) ? items : []).filter(task => {
    const due = dueDay(task);
    return isWeeklyRock(task, ids) || !due || !day || due <= day;
  });
  return eligible.slice().sort((a, b) =>
    rank(a) - rank(b) ||
    Number(b?.priority || 0) - Number(a?.priority || 0) ||
    (dueDay(a) || '9999-12-31').localeCompare(dueDay(b) || '9999-12-31') ||
    String(a?.title || '').localeCompare(String(b?.title || ''))
  );
}
