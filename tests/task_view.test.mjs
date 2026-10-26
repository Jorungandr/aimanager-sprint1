import test from 'node:test';
import assert from 'node:assert/strict';
import { localDay, taskWindow, matchesTaskScope, selectTaskView } from '../task-view.mjs';

const window = { today: '2026-12-30', near: '2027-01-02' };
const tasks = [
    { id: 3, title: 'API review', status: 'doing', assignee_user_id: 7, requirement_id: 2, due_date: '2026-12-29' },
    { id: 1, title: 'UI review', status: 'todo', assignee_user_id: null, requirement_id: 2, due_date: '2027-01-02' },
    { id: 2, title: 'Release', status: 'done', assignee_user_id: 7, requirement_id: null, due_date: '2026-12-28' },
    { id: 4, title: 'No date', status: 'backlog', assignee_user_id: null, requirement_id: null, due_date: null },
];
const requirements = new Map([[2, { title: 'Archived Feature', archived: true }]]);

test('local calendar window crosses year without UTC date conversion', () => {
    assert.equal(localDay(new Date(2026, 11, 30, 23, 59)), '2026-12-30');
    assert.deepEqual(taskWindow(new Date(2026, 11, 30, 23, 59)), window);
});
test('completed tasks are never overdue or due soon', () => {
    assert.equal(matchesTaskScope(tasks[2], 'overdue', 7, window), false);
    assert.equal(matchesTaskScope({ ...tasks[2], due_date: window.today }, 'due_soon', 7, window), false);
});
test('due soon includes today and day three but excludes past dates and day four', () => {
    for (const due of ['2026-12-30', '2027-01-02']) {
        assert.equal(matchesTaskScope({ ...tasks[1], due_date: due }, 'due_soon', 7, window), true);
    }
    for (const due of ['2026-12-29', '2027-01-03', null]) {
        assert.equal(matchesTaskScope({ ...tasks[1], due_date: due }, 'due_soon', 7, window), false);
    }
});
test('mine requires an actual identity and supports numeric API IDs', () => {
    assert.equal(matchesTaskScope(tasks[0], 'mine', '7', window), true);
    assert.equal(matchesTaskScope(tasks[1], 'mine', null, window), false);
});
test('search includes archived linked requirements and combines with assignee and scope', () => {
    const result = selectTaskView(tasks, requirements, { search: '  ARCHIVED  ', assignee: '7', scope: 'overdue' }, 7, window);
    assert.deepEqual(result.map(task => task.id), [3]);
});
test('unassigned scope excludes every assigned task', () => {
    assert.deepEqual(selectTaskView(tasks, requirements, { scope: 'unassigned' }, 7, window).map(task => task.id), [1, 4]);
});
test('due ordering puts undated tasks last and resolves ties deterministically', () => {
    const data = [...tasks, { ...tasks[1], id: 5 }];
    assert.deepEqual(selectTaskView(data, requirements, { sort: 'due' }, 7, window).map(task => task.id), [2, 3, 1, 5, 4]);
});
test('sorting and filtering never mutate the project task array', () => {
    const before = structuredClone(tasks);
    assert.deepEqual(selectTaskView(tasks, requirements, { sort: 'newest' }, 7, window).map(task => task.id), [4, 3, 2, 1]);
    assert.deepEqual(tasks, before);
});
