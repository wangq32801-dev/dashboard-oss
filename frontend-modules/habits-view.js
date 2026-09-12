// Pure habit calculations shared by Today and the habits surface.
export function habitScore(habit) {
  if (!habit) return 0;
  if (habit.type === 'Boolean') {
    const days = Object.values(habit.daily || {}).filter(value => value > 0).length;
    return Math.min(100, Math.round(days / 7 * 100));
  }
  const weeklyGoal = (habit.name || '').includes('骑行') ? 300 : ((habit.goal || 1) * 7);
  return Math.min(100, Math.round((habit.weekTotal || 0) / Math.max(1, weeklyGoal) * 100));
}

export function habitCompletionPercent(habits) {
  const list = Array.isArray(habits) ? habits : [];
  if (!list.length) return 0;
  return Math.round(list.filter(habit => habit && habit.checked).length / list.length * 100);
}
