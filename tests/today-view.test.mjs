import assert from 'node:assert/strict';
import {isWeeklyRock, selectTodayFocusTasks} from '../frontend-modules/today-view.js';

const today = '2026-09-04';
const tasks = [
  {id: 'future', title: 'future priority five', due: '2026-09-20', priority: 5, quad: 'q1'},
  {id: 'undated', title: 'undated q2', priority: 3, quad: 'q2'},
  {id: 'today', title: 'due today', due: today, priority: 1, quad: 'q3'},
  {id: 'overdue', title: 'overdue', due: '2026-09-03', priority: 1, quad: 'q4'},
  {id: 'rock', title: 'weekly rock', due: '2026-09-20', priority: 1, quad: 'q4'},
];

assert.equal(isWeeklyRock(tasks[0], new Set(['rock'])), false, 'priority must never imply weekly-rock identity');
assert.equal(isWeeklyRock(tasks[4], new Set(['rock'])), true, 'week-plan id identifies a weekly rock');

const selected = selectTodayFocusTasks(tasks, {today, rockIds: new Set(['rock'])});
assert.deepEqual(selected.map(task => task.id), ['rock', 'overdue', 'today', 'undated']);
assert.equal(selected.some(task => task.id === 'future'), false, 'ordinary future tasks stay out of Today');

const tagged = selectTodayFocusTasks([
  {id: 'tagged', title: 'tagged', due: '2026-10-01', tags: ['大石头']},
], {today});
assert.deepEqual(tagged.map(task => task.id), ['tagged']);

console.log('today view selection contract: ok');
