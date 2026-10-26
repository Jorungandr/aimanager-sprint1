export function localDay(value) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

export function taskWindow(now = new Date()) {
    const near = new Date(now);
    near.setDate(near.getDate() + 3);
    return { today: localDay(now), near: localDay(near) };
}

export function matchesTaskScope(task, scope, userId, window = taskWindow()) {
    if (scope === 'mine') {
        return userId !== null && userId !== undefined && task.assignee_user_id !== null
            && task.assignee_user_id !== undefined && String(task.assignee_user_id) === String(userId);
    }
    if (scope === 'unassigned') {
        return task.assignee_user_id === null || task.assignee_user_id === undefined;
    }
    if (scope === 'overdue') {
        return task.status !== 'done' && Boolean(task.due_date) && task.due_date < window.today;
    }
    if (scope === 'due_soon') {
        return task.status !== 'done' && Boolean(task.due_date)
            && task.due_date >= window.today && task.due_date <= window.near;
    }
    return true;
}

export function selectTaskView(tasks, requirementMap, filters, userId, window = taskWindow()) {
    const search = (filters.search || '').trim().toLowerCase();
    const visible = tasks.filter(task => {
        const requirement = requirementMap.get(task.requirement_id);
        const text = `${task.title} ${requirement?.title || ''}`.toLowerCase();
        const assignee = !filters.assignee || (filters.assignee === 'unassigned'
            ? task.assignee_user_id === null || task.assignee_user_id === undefined
            : String(task.assignee_user_id) === filters.assignee);
        return text.includes(search) && assignee && matchesTaskScope(task, filters.scope, userId, window);
    });
    if (filters.sort === 'due') {
        visible.sort((first, second) => {
            if (!first.due_date && second.due_date) {
                return 1;
            }
            if (first.due_date && !second.due_date) {
                return -1;
            }
            return (first.due_date || '').localeCompare(second.due_date || '') || first.id - second.id;
        });
    } else if (filters.sort === 'newest') {
        visible.sort((first, second) => second.id - first.id);
    }
    return visible;
}
