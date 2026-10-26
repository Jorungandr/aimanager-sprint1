import { localDay, taskWindow, matchesTaskScope, selectTaskView } from './task-view.mjs';
/* 页面展示与请求。所有真正的权限判断由服务器完成。 */
const $ = id => document.getElementById(id);
let current = null, roles = [], roleOptions = [], users = [], projects = [];
const permissionLabels = { 'project.view': '查看项目', 'members.manage': '管理成员', 'projects.manage': '管理项目', 'roles.manage': '管理角色', 'workitems.manage': '管理需求任务' };
const statusLabels = { backlog: '待梳理', todo: '待办', doing: '进行中', done: '已完成' };
let activeProject = '', activeView = 'collaboration', projectRequest = 0, editingTask = null, editingRequirement = null, dialogOpener = null;
const filters = new Map();
/* 折叠面板展开状态：仅页面内存，按项目隔离；退出登录时随 filters 一起清空，不恢复旧账号界面状态 */
const panelState = new Map();
const creationDrafts = new Map();
const dialogSnapshots = new WeakMap();
let noticeTimer;
function panelOpen(projectId, name) {
  return panelState.get(projectId)?.[name] === true;
}
function recordPanel(projectId, name, open) {
  const state = panelState.get(projectId) || {};
  state[name] = open;
  panelState.set(projectId, state);
}
let csrfRequest = null;
let refreshRequest = 0;
let sessionRevision = 0;
let permissionCheckRequest = 0;
/* 服务端错误统一转成面向用户的提示：不把状态码抛给用户，保留业务原因。 */
const statusFallback = {
  400: '提交内容不符合要求，请检查后重试。',
  401: '登录已过期，请重新登录。',
  403: '没有执行该操作的权限，请联系项目管理者。',
  404: '目标数据不存在，可能已被其他操作移除。',
  405: '当前操作不受支持。',
  409: '数据已被其他操作修改，请刷新后重试。',
  428: '页面数据已过期，请刷新后重试。',
  429: '操作过于频繁，请稍后再试。',
};
const endSentence = text => (/[。！？：；]$/.test(text) ? text : `${text}。`);
function describeError(status, message) {
  const detail = String(message || '').trim();
  if (status === 400) return detail ? `无法保存：${endSentence(detail)}` : statusFallback[400];
  if (status === 403) return detail ? `权限不足：${endSentence(detail)}` : statusFallback[403];
  // 冲突与过期：后端已给出具体原因，补一句“输入保留、不会覆盖”，避免用户重复提交旧版本
  if (status === 409) return detail ? `${endSentence(detail)}本次输入已保留，不会覆盖他人的修改。` : statusFallback[409];
  if (status === 428) return detail ? `${endSentence(detail)}请关闭弹窗后点击“刷新数据”再保存。` : statusFallback[428];
  return detail ? endSentence(detail) : (statusFallback[status] || '操作失败，请稍后重试。');
}
function closeDialogs() {
  for (const id of ['taskDialog', 'requirementDialog']) {
    const dialog = $(id);
    if (dialog.open) dialog.close();
    const form = dialog.querySelector('form');
    if (form) delete form.dataset.conflict;
    dialog.querySelectorAll('[data-dialog-refresh]').forEach(button => { button.hidden = true; });
  }
  editingTask = null;
  editingRequirement = null;
  dialogOpener = null;
}
/* 登录过期统一收口：清空编辑上下文、关闭弹窗、回到登录页并说明原因。 */
function enterLoggedOut(expired = true) {
  refreshRequest += 1;
  sessionRevision += 1;
  current = null;
  roles = [];
  roleOptions = [];
  users = [];
  projects = [];
  syncShell();
  projectRequest += 1;
  closeDialogs();
  filters.clear();
  panelState.clear();
  creationDrafts.clear();
  activeProject = '';
  $('workspace').hidden = true;
  $('logout').hidden = true;
  $('setupPanel').hidden = true;
  $('loginPanel').hidden = false;
  $('projects').replaceChildren();
  $('projectPicker').replaceChildren(option('', '暂无项目'));
  $('projectPicker').disabled = true;
  $('currentUser').textContent = '—';
  $('currentUser').title = '';
  $('roleCount').textContent = '0';
  $('projectCount').textContent = '0';
  for (const id of ['roleForm', 'userForm', 'projectForm']) {
    $(id).reset();
  }
  renderCheckProjects();
  $('checkResult').textContent = '';
  renderMatrix();
  say(expired ? '登录已过期，请重新登录。未保存的编辑内容已关闭。' : '已安全退出', expired);
}
async function api(path, options = {}) {
    const revision = sessionRevision;
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (['POST', 'PUT', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
        if (!csrfRequest) {
            csrfRequest = api('/api/csrf', { cache: 'no-store' }).then(data => {
                if (!data.csrf_token) {
                    throw new Error('服务器未返回安全令牌。');
                }
                return data.csrf_token;
            }).finally(() => {
                csrfRequest = null;
            });
        }
        headers['X-CSRF-Token'] = await csrfRequest;
    }
    let response;
    try {
        response = await fetch(path, { ...options, credentials: 'same-origin', headers });
    } catch (cause) {
        throw new Error('无法连接服务器，请检查服务是否启动后重试。', { cause });
    }
    let data;
    try {
        data = await response.json();
    } catch (cause) {
        throw new Error('服务器响应格式异常，请刷新重试。', { cause });
    }
    if (!response.ok) {
        if (response.status === 401 && current && revision === sessionRevision) {
            enterLoggedOut();
        }
        const error = new Error(describeError(response.status, data.error));
        error.status = response.status;
        error.reason = data.error || '';
        throw error;
    }
    return data;
}
function say(message, error = false) {
  const notice = $('notice');
    clearTimeout(noticeTimer);
    notice.replaceChildren(document.createTextNode(message));
    notice.className = error ? 'error' : 'ok';
    if (message) {
        const dismiss = el('button', 'notice-dismiss', '关闭');
        dismiss.type = 'button';
        dismiss.setAttribute('aria-label', '关闭提示');
        dismiss.addEventListener('click', () => {
            clearTimeout(noticeTimer);
            notice.replaceChildren();
        });
        notice.append(dismiss);
        if (!error) {
            noticeTimer = setTimeout(() => notice.replaceChildren(), 7000);
        }
    }
}
/* 进行中保护：请求期间禁用按钮，防止连点产生重复写请求。
   失败/取消路径都不会残留禁用态；refresh() 重建 DOM 后旧按钮已脱离页面，无需也不应再改它的状态。 */
async function runGuarded(button, action) {
  if (button.disabled) {
    return;
  }
  button.disabled = true;
  try {
    await action();
  } finally {
    if (button.isConnected) {
      button.disabled = false;
    }
  }
}
function formData(form) { return Object.fromEntries(new FormData(form).entries()); }
async function submit(form, path, after, method = 'POST') {
  if (form.dataset.submitting === 'true') {
    return;
  }
  const revision = sessionRevision;
  form.dataset.submitting = 'true';
  let problem = form.querySelector('[data-form-error]');
  if (!problem) {
    problem = document.createElement('p');
    problem.className = 'error';
    problem.dataset.formError = '';
    problem.setAttribute('role', 'alert');
    form.append(problem);
  }
  problem.textContent = '';
  const buttons = [...form.querySelectorAll('button')];
  const labels = buttons.map(button => button.textContent);
  const saveButton = form.querySelector('button:not([type]), button[type=submit]');
  if (saveButton) {
    saveButton.textContent = '保存中…';
  }
  buttons.forEach(button => { button.disabled = true; });
  try {
    const created = await api(path, { method, body: JSON.stringify(formData(form)) });
    if (revision !== sessionRevision) {
        return;
    }
    if (form.dataset.draftKey) {
        creationDrafts.delete(form.dataset.draftKey);
    }
    if (form.hasAttribute('data-create-task')) {
        const projectId = Number(form.dataset.draftProject);
        const state = filters.get(projectId);
        if (state) {
            Object.assign(state, { search: '', assignee: '', scope: 'all', sort: 'newest' });
        }
    }
    if (path === '/api/projects') {
      activeProject = String(created.id);
      activeView = 'collaboration';
    }
    form.reset();
    say('保存成功，数据已更新');
    await after();
  } catch (error) {
    if (revision !== sessionRevision) {
        return;
    }
    problem.textContent = error.message;
    say(error.message, true);
  } finally {
    delete form.dataset.submitting;
    buttons.forEach((button, index) => { button.disabled = false; button.textContent = labels[index]; });
  }
}
function cell(text) { const td = document.createElement('td'); td.textContent = text; return td; }
function option(value, text) { const item = document.createElement('option'); item.value = value; item.textContent = text; return item; }
async function refresh() {
  const request = ++refreshRequest;
  let account;
  try {
    account = await api('/api/me');
  } catch (error) {
    if (error.status !== 401) {
      throw error;
    }
    account = null;
  }
  if (request !== refreshRequest) {
    return;
  }
  current = account;
  syncShell();
  $('setupPanel').hidden = true; $('loginPanel').hidden = true; $('workspace').hidden = !current; $('logout').hidden = !current;
  if (!current) {
    const status = await api('/api/status');
    if (request === refreshRequest) {
      $(status.needs_setup ? 'setupPanel' : 'loginPanel').hidden = false;
    }
    return;
  }
  $('currentUser').textContent = current.email;
  $('currentUser').title = current.email;
  const canManageRoles = current.can_manage_roles;
  const canManageProjects = current.can_manage_projects;
  $('adminPanel').hidden = !(current.is_admin || canManageRoles || canManageProjects);
  $('roleForm').hidden = !canManageRoles; $('matrixPanel').hidden = !canManageRoles;
  $('projectForm').hidden = !canManageProjects; $('userForm').hidden = !current.is_admin;
  const [nextRoles, nextProjects, nextUsers] = await Promise.all([
    canManageRoles ? api('/api/roles') : [],
    api('/api/projects'),
    account.is_admin ? api('/api/users') : (account.can_manage_members ? api('/api/user-options') : []),
  ]);
  if (request !== refreshRequest) {
    return;
  }
  const nextOptions = nextProjects.length || canManageRoles ? await api('/api/role-options') : [];
  if (request !== refreshRequest) {
    return;
  }
  roles = nextRoles;
  projects = nextProjects;
  users = nextUsers;
  roleOptions = nextOptions;
  $('roleCount').textContent = roleOptions.length; $('projectCount').textContent = projects.length;
  if (!projects.some(project => String(project.id) === activeProject)) {
    activeProject = projects.length ? String(projects[0].id) : '';
  }
  // 无项目时保留占位项并禁用，避免出现空白下拉框
  if (projects.length) {
    $('projectPicker').replaceChildren(...projects.map(project => option(project.id, project.name)));
    $('projectPicker').value = activeProject;
  } else {
    $('projectPicker').replaceChildren(option('', '暂无项目'));
  }
  $('projectPicker').disabled = !projects.length;
  // 提示文案属于界面增强，旧页面结构没有该节点时跳过，不影响数据渲染
  const pickerHint = $('pickerHint');
  if (pickerHint) {
    pickerHint.textContent = projects.length
      ? '成员、需求、看板与数据概览均跟随当前项目。'
      : '当前账号还没有可访问的项目，因此没有可切换的选项。请由项目管理者创建项目并把你加入成员。';
  }
  renderMatrix();
  setView(activeView);
  await renderProjects();
  if (request !== refreshRequest) {
    return;
  }
  renderCheckProjects();
  if (activeView === 'insights') {
    await renderInsights();
  }
}
function setView(view) {
  const canAdmin = current && (current.is_admin || current.can_manage_roles || current.can_manage_projects);
  activeView = view === 'administration' && !canAdmin ? 'collaboration' : view;
  const titles = {
    collaboration: ['项目协作', '把分工落到任务，让进展清晰可见。'],
    insights: ['数据概览', '从交付进度到到期风险，掌握项目全貌。'],
    administration: ['管理配置', '为团队建立清晰的角色、权限与项目边界。'],
    verification: ['权限验证', '核对当前账号的项目访问与成员管理权限。'],
  };
  const title = titles[activeView] || titles.collaboration;
  $('pageTitle').textContent = title[0];
  $('pageDescription').textContent = title[1];
  $('navToggle').setAttribute('aria-expanded', 'false');
  $('navToggle').textContent = '展开导航';
  if (activeView !== 'insights') {
    insightRequest += 1;
    insightDrillRequest += 1;
  }
  $('collaborationPanel').hidden = activeView !== 'collaboration';
  $('insightsPanel').hidden = activeView !== 'insights';
  $('adminPanel').hidden = activeView !== 'administration' || !canAdmin;
  $('checkPanel').hidden = activeView !== 'verification';
  document.querySelectorAll('[data-view]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === activeView));
    button.hidden = button.dataset.view === 'administration' && !canAdmin;
  });
}
function syncShell() {
    document.body.classList.toggle('workspace-ready', Boolean(current));
    $('authLayout').hidden = Boolean(current);
    if (!current) {
        insightRequest += 1;
        insightDrillRequest += 1;
        $('insightBody').replaceChildren();
        $('insightBody').removeAttribute('aria-busy');
        $('pageTitle').textContent = '让团队协作，有条不紊。';
        $('pageDescription').textContent = '把需求、任务和进展，放在一个清晰的工作空间。';
    } else {
        const initial = current.email.slice(0, 1).toUpperCase();
        document.querySelector('.account-avatar').textContent = initial;
    }
}
async function allowed(path) {
  try {
    await api(path);
    return true;
  } catch (error) {
    if (error.status === 403) {
      return false;
    }
    throw error;
  }
}
function renderMatrix() {
  const header = $('matrix').querySelector('thead tr');
  header.replaceChildren();
  for (const title of ['角色', ...Object.values(permissionLabels), '操作']) {
    const th = document.createElement('th');
    th.textContent = title;
    th.scope = 'col';
    header.append(th);
  }
  const body = $('matrix').querySelector('tbody'); body.replaceChildren();
  for (const role of roles) {
    const tr = document.createElement('tr'); tr.append(cell(role.name)); const checks = {};
    for (const key of Object.keys(permissionLabels)) {
      const td = document.createElement('td'), check = document.createElement('input'); check.type = 'checkbox'; check.checked = role.permissions.includes(key);
      check.setAttribute('aria-label', `${role.name} ${permissionLabels[key]}`); checks[key] = check; td.append(check); tr.append(td);
    }
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.textContent = '保存权限';
    button.addEventListener('click', async () => {
      await runGuarded(button, async () => {
        try {
          await api(`/api/roles/${role.id}/permissions`, { method: 'PUT', body: JSON.stringify({ permissions: Object.keys(checks).filter(k => checks[k].checked) }) });
          say('权限已保存');
          await refresh();
        } catch (e) {
          say(e.message, true);
        }
      });
    });
    action.append(button);
    tr.append(action);
    body.append(tr);
  }
}
function workspaceLoading(name) {
    const loading = el('section', 'workspace-loading');
    loading.setAttribute('role', 'status');
    loading.append(el('p', 'loading-label', `正在加载${name}…`));
    const preview = el('div', 'loading-preview');
    preview.setAttribute('aria-hidden', 'true');
    for (let index = 0; index < 4; index += 1) {
        preview.append(el('span', 'loading-placeholder'));
    }
    loading.append(preview);
    return loading;
}
async function renderProjects() {
    const request = ++projectRequest;
    const account = current;
    const projectId = activeProject;
    const target = $('projects');
    const valid = () => request === projectRequest && current === account && projectId === activeProject;
    target.removeAttribute('aria-busy');
    target.replaceChildren();
    if (!projects.length) {
        const empty = el('div', 'empty-state');
        empty.textContent = '当前账号还没有可访问的项目。管理员可在“管理配置”中创建项目并把成员加入项目；普通成员请联系项目管理者分配权限。';
        target.append(empty);
        return;
    }
    const project = projects.find(item => String(item.id) === projectId);
    if (!project || !account) {
        return;
    }
    target.setAttribute('aria-busy', 'true');
    target.append(workspaceLoading(project.name));
    try {
        const [members, requirements, tasks, canManageMembers, canManageWorkitems] = await Promise.all([
            api(`/api/projects/${project.id}/members`),
            api(`/api/projects/${project.id}/requirements`),
            api(`/api/projects/${project.id}/tasks`),
            allowed(`/api/projects/${project.id}/manage-check`),
            allowed(`/api/projects/${project.id}/workitems/manage-check`),
        ]);
        if (!valid()) {
            return;
        }
        const assignableRoles = canManageMembers ? await api(`/api/projects/${project.id}/role-options`) : [];
        if (!valid()) {
            return;
        }
        const card = el('section', 'project');
        card.append(el('h3', null, project.name));
        renderMembers(card, project, members, canManageMembers, assignableRoles);
        renderWorkItems(card, project, members, requirements, tasks, canManageWorkitems);
        renderProjectIntroduction(card, project, members, requirements, tasks, canManageWorkitems);
        target.replaceChildren(card);
    } catch (error) {
        if (!valid()) {
            return;
        }
        const problem = el('section', 'workspace-load-error');
        problem.setAttribute('role', 'alert');
        problem.append(el('h3', null, '项目暂时未能加载'), el('p', 'error', error.message));
        const retry = el('button', 'secondary', '重试加载项目');
        retry.type = 'button';
        retry.addEventListener('click', () => renderProjects().catch(cause => say(cause.message, true)));
        problem.append(retry);
        target.replaceChildren(problem);
    } finally {
        if (valid()) {
            target.removeAttribute('aria-busy');
        }
    }
}
function renderProjectIntroduction(card, project, members, requirements, tasks, canManage) {
    const completed = tasks.filter(task => task.status === 'done').length;
    const remaining = tasks.filter(task => task.status !== 'done');
    const today = new Date();
    const near = new Date(today);
    near.setDate(near.getDate() + 3);
    const todayKey = localDay(today);
    const risks = remaining.filter(task => task.due_date && task.due_date <= localDay(near));
    const candidates = risks.length ? risks : remaining;
    const queue = [...candidates].sort((first, second) => {
        const firstRank = first.status === 'doing' ? 0 : 1;
        const secondRank = second.status === 'doing' ? 0 : 1;
        return (first.due_date || '9999').localeCompare(second.due_date || '9999')
            || firstRank - secondRank || first.id - second.id;
    }).slice(0, 3);
    const introduction = el('div', 'workspace-introduction');
    const hero = el('section', 'project-hero');
    hero.setAttribute('aria-label', '项目进度概览');
    const art = el('img', 'project-hero-art');
    art.src = 'assets/workspace-hummingbird.png';
    art.alt = '';
    art.width = 2172;
    art.height = 724;
    const content = el('div', 'project-hero-content');
    const title = card.querySelector(':scope > h3');
    content.append(title);
    const message = !tasks.length ? '从第一条任务开始，让想法逐步成为交付。'
        : (remaining.length ? `${remaining.length} 项任务正在等待推进，每一步协作都有迹可循。`
            : '本轮任务已全部完成，准备迎接下一次交付。');
    content.append(el('p', 'project-hero-message', message));
    const action = el('button', 'hero-action', canManage ? '新建任务' : '查看项目进展');
    action.type = 'button';
    action.addEventListener('click', () => {
        if (canManage) {
            const form = card.querySelector('[data-create-task]');
            card.querySelector('[data-create-kind=task]')?.click();
            const details = form.closest('details');
            details.open = true;
            recordPanel(project.id, 'create', true);
            form.querySelector('[name="title"]').focus();
        } else {
            setView('insights');
            renderInsights().catch(error => say(error.message, true));
        }
    });
    content.append(action);
    const pulse = el('div', 'project-pulse');
    const percent = tasks.length ? Math.round(completed * 100 / tasks.length) : 0;
    const completion = el('div', 'pulse-completion');
    completion.append(el('strong', null, `${percent}%`), el('span', null, '任务完成率'));
    pulse.append(completion, card.querySelector('.progress-summary'));
    const people = el('div', 'pulse-team');
    people.append(el('strong', null, `${members.length} 人协作`),
        el('span', null, `${requirements.filter(item => !item.archived).length} 条活跃需求`));
    pulse.append(people);
    hero.append(art, content, pulse);
    const focus = el('aside', 'project-focus');
    focus.setAttribute('aria-label', '项目关注事项');
    const focusHeading = el('div', 'focus-heading');
    focusHeading.append(el('h4', null, risks.length ? '需要关注' : '下一步任务'),
        el('span', 'focus-count', `${risks.length || remaining.length} 项`));
    focus.append(focusHeading);
    const overdue = risks.filter(task => task.due_date < todayKey).length;
    focus.append(el('p', 'focus-description', risks.length
        ? `${overdue} 项逾期 · ${risks.length - overdue} 项在 3 天内到期`
        : (remaining.length ? '为正在推进的工作，留出专注时间。' : '当前任务均已完成，可以准备下一轮计划。')));
    const list = el('div', 'focus-task-list');
    for (const task of queue) {
        const text = `${task.title} · ${task.due_date || statusLabels[task.status]}`;
        const button = el('button', 'focus-task');
        button.append(el('span', 'focus-task-title', task.title));
        const meta = el('span', 'focus-task-meta');
        const status = el('span', `focus-status focus-status-${task.status}`, statusLabels[task.status]);
        meta.append(status);
        if (task.due_date) {
            meta.append(el('span', 'focus-due-date', `${task.due_date < todayKey ? '已逾期 · ' : '截止 '}${task.due_date}`));
        }
        button.append(meta);
        button.type = 'button';
        button.title = text;
        button.classList.toggle('is-overdue', Boolean(task.due_date && task.due_date < todayKey));
        button.addEventListener('click', () => {
            if (canManage) {
                openTaskEditor(project, task, members, requirements);
                return;
            }
            const state = filters.get(project.id);
            state.search = task.title;
            state.assignee = '';
            state.scope = 'all';
            renderProjects().then(() => {
                $('projects').querySelector('.board')?.scrollIntoView({ block: 'start' });
            }).catch(error => say(error.message, true));
        });
        list.append(button);
    }
    if (!queue.length) {
        list.append(el('p', 'focus-empty', tasks.length ? '本轮任务已全部完成。' : '创建任务后，这里会显示待推进事项。'));
    }
    const more = el('button', 'focus-more', '查看完整数据概览');
    more.type = 'button';
    more.addEventListener('click', () => {
        setView('insights');
        renderInsights().catch(error => say(error.message, true));
    });
    focus.append(list, more);
    introduction.append(hero, focus);
    card.prepend(introduction);
}
function renderMembers(card, project, members, canManage, assignableRoles) {
  const details = document.createElement('details');
  if (panelOpen(project.id, 'members')) {
    details.open = true;
  }
  details.addEventListener('toggle', () => recordPanel(project.id, 'members', details.open));
  const summary = document.createElement('summary');
  summary.textContent = `项目成员 · ${members.length} 人${canManage ? ' · 展开管理' : ''}`;
  details.append(summary);
  card.append(details);
  card = details;
  const list = document.createElement('div'); list.className = 'member-list';
  if (!members.length) list.textContent = '暂无成员';
  for (const member of members) {
    const row = document.createElement('p'); row.textContent = `${member.email} · ${member.role_name} `;
    if (canManage && !member.is_owner) {
      const remove = document.createElement('button'); remove.className = 'danger'; remove.textContent = '移出项目';
      remove.addEventListener('click', async () => {
        if (!confirm(`移出 ${member.email}？`)) return;
        await runGuarded(remove, async () => {
          try {
            await api(`/api/projects/${project.id}/members/${member.user_id}`, { method: 'DELETE' });
            say('成员已移出，权限已回收');
            await refresh();
          } catch (e) {
            say(e.message, true);
          }
        });
      });
      row.append(remove);
    }
    list.append(row);
  }
  card.append(list);
  if (!canManage) return;
  const form = document.createElement('form'); form.className = 'inline compact-form';
  const userSelect = document.createElement('select'); userSelect.name = 'user_id'; userSelect.setAttribute('aria-label', '选择账号'); users.forEach(u => userSelect.append(option(u.id, u.email)));
  const roleSelect = document.createElement('select'); roleSelect.name = 'role_id'; roleSelect.setAttribute('aria-label', '选择可授权角色'); assignableRoles.forEach(r => roleSelect.append(option(r.id, r.name)));
  const add = document.createElement('button'); add.textContent = '加入并指派角色'; form.append(userSelect, roleSelect, add);
  add.disabled = !assignableRoles.length || !users.length;
  form.addEventListener('submit', event => { event.preventDefault(); submit(form, `/api/projects/${project.id}/members`, refresh); }); card.append(form);
}
function retainCreationDraft(form, projectId, kind) {
    const key = `${projectId}:${kind}`;
    form.dataset.draftKey = key;
    form.dataset.draftProject = String(projectId);
    const draft = creationDrafts.get(key);
    if (draft) {
        for (const [name, value] of Object.entries(draft)) {
            const field = form.elements.namedItem(name);
            if (field) {
                field.value = value;
            }
        }
    }
    const remember = () => creationDrafts.set(key, formData(form));
    form.addEventListener('input', remember);
    form.addEventListener('change', remember);
}
function renderWorkItems(card, project, members, requirements, tasks, canManage) {
  const heading = document.createElement('h4'); heading.textContent = '需求与任务看板'; card.append(heading);
  const completed = tasks.filter(task => task.status === 'done').length;
  const progress = document.createElement('div');
  progress.className = 'progress-summary';
  const progressText = document.createElement('p');
  progressText.textContent = `${tasks.length} 项任务 · 已完成 ${completed} 项 · ${canManage ? '可管理任务' : '只读访问'}`;
  const meter = document.createElement('progress');
  meter.max = tasks.length || 1;
  meter.value = completed;
  meter.setAttribute('aria-label', '任务完成进度');
  progress.append(progressText, meter);
  card.append(progress);
  const openRequirements = requirements.filter(item => !item.archived);
  const archivedCount = requirements.length - openRequirements.length;
  const requirementDetails = document.createElement('details');
  // 空列表首次访问默认展开（延续原行为）；用户手动折叠后尊重其选择
  const panel = panelState.get(project.id) || {};
  if (panel.requirements === undefined) {
    requirementDetails.open = !requirements.length;
  } else {
    requirementDetails.open = panel.requirements;
  }
  requirementDetails.addEventListener('toggle', () => recordPanel(project.id, 'requirements', requirementDetails.open));
  const requirementSummary = document.createElement('summary');
  requirementSummary.textContent = `需求列表 · ${requirements.length} 条`;
  // 归档数量用 title 补充说明，不改变 summary 文本本身
  if (archivedCount) requirementSummary.title = `其中 ${archivedCount} 条已归档：仅保留历史任务的关联，不可用于新任务`;
  requirementDetails.append(requirementSummary);
  if (!requirements.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state compact';
    empty.textContent = '还没有需求条目。需求用于归类任务，可以先建需求，也可以直接建任务。';
    requirementDetails.append(empty);
  } else {
    const requirementList = document.createElement('ul'); requirementList.className = 'requirements';
    for (const item of requirements) {
      const li = document.createElement('li');
      if (item.archived) li.className = 'is-archived';
      const strong = document.createElement('strong');
      strong.textContent = item.title;
      li.append(strong);
      if (item.archived) {
        const badge = document.createElement('span');
        badge.className = 'badge-archived';
        badge.textContent = '已归档';
        badge.title = '已归档需求不参与新任务关联，历史任务的关联会保留';
        li.append(badge);
      }
      if (item.description) li.append(document.createTextNode(` — ${item.description}`));
      if (canManage) {
        const actions = document.createElement('div');
        actions.className = 'requirement-actions';
        if (item.archived) {
          // 已归档：只提供恢复，避免用户在归档态继续编辑
          const restore = document.createElement('button');
          restore.type = 'button';
          restore.textContent = '恢复';
          restore.addEventListener('click', async () => {
            await runGuarded(restore, async () => {
              try {
                await api(`/api/projects/${project.id}/requirements/${item.id}/restore`, { method: 'POST', body: JSON.stringify({ version: item.version }) });
                say('需求已恢复，可以重新关联任务');
                await refresh();
              } catch (error) {
                say(error.message, true);
              }
            });
          });
          actions.append(restore);
        } else {
          const edit = document.createElement('button');
          edit.type = 'button';
          edit.className = 'secondary';
          edit.textContent = '编辑';
          edit.addEventListener('click', () => openRequirementEditor(project, item));
          const archive = document.createElement('button');
          archive.type = 'button';
          archive.className = 'danger';
          archive.textContent = '归档';
          archive.addEventListener('click', async () => {
            if (!confirm(`归档需求"${item.title}"？历史任务将保留关联。`)) return;
            await runGuarded(archive, async () => {
              try {
                await api(`/api/projects/${project.id}/requirements/${item.id}/archive`, { method: 'POST', body: JSON.stringify({ version: item.version }) });
                say('需求已归档，历史任务仍保留关联');
                await refresh();
              } catch (error) {
                say(error.message, true);
              }
            });
          });
          actions.append(edit, archive);
        }
        li.append(actions);
      }
      requirementList.append(li);
    }
    requirementDetails.append(requirementList);
  }
  card.append(requirementDetails);
  if (canManage) {
    const forms = document.createElement('div'); forms.className = 'workitem-forms';
    const requirementForm = document.createElement('form'); requirementForm.innerHTML = '<h5>新增需求</h5><label>标题<input name="title" maxlength="80" required></label><label>说明<textarea name="description" maxlength="500" rows="3" placeholder="可选；说明背景、边界或验收要点"></textarea></label><button>保存需求</button>';
    requirementForm.addEventListener('submit', event => { event.preventDefault(); submit(requirementForm, `/api/projects/${project.id}/requirements`, refresh); });
    const taskForm = document.createElement('form'); taskForm.dataset.createTask = ''; const taskTitle = document.createElement('h5'); taskTitle.textContent = '新增任务'; taskForm.append(taskTitle);
    const titleLabel = document.createElement('label'); titleLabel.textContent = '标题'; const titleInput = document.createElement('input'); titleInput.name = 'title'; titleInput.maxLength = 100; titleInput.required = true; titleLabel.append(titleInput); taskForm.append(titleLabel);
    const requirementLabel = document.createElement('label'); requirementLabel.textContent = '关联需求'; const requirementSelect = document.createElement('select'); requirementSelect.name = 'requirement_id'; requirementSelect.append(option('', '不关联')); openRequirements.forEach(item => requirementSelect.append(option(item.id, item.title))); requirementLabel.append(requirementSelect); taskForm.append(requirementLabel);
    // 归档需求不进入新任务选项；全部归档时说明原因（不加占位 option，避免被误当成可选项）
    const requirementHint = document.createElement('p');
    requirementHint.className = 'hint';
    requirementHint.textContent = openRequirements.length
      ? '只能选择未归档的需求；已归档需求需先恢复。'
      : (requirements.length ? '当前需求均已归档，新任务暂时无法关联需求；请先恢复需求。' : '还没有需求可关联，可先保存需求再创建任务，也可以选择“不关联”。');
    taskForm.append(requirementHint);
    const assigneeLabel = document.createElement('label'); assigneeLabel.textContent = '负责人'; const assigneeSelect = document.createElement('select'); assigneeSelect.name = 'assignee_user_id'; assigneeSelect.append(option('', '暂不指派')); members.forEach(item => assigneeSelect.append(option(item.user_id, item.email))); assigneeLabel.append(assigneeSelect); taskForm.append(assigneeLabel);
    const dueLabel = document.createElement('label'); dueLabel.textContent = '截止日'; const dueInput = document.createElement('input'); dueInput.type = 'date'; dueInput.name = 'due_date'; dueLabel.append(dueInput); taskForm.append(dueLabel);
    const status = document.createElement('input'); status.type = 'hidden'; status.name = 'status'; status.value = 'todo'; taskForm.append(status);
    const create = document.createElement('button'); create.textContent = '保存任务'; taskForm.append(create);
    taskForm.addEventListener('submit', event => { event.preventDefault(); submit(taskForm, `/api/projects/${project.id}/tasks`, refresh); });
    const createDetails = document.createElement('details');
    if (panelOpen(project.id, 'create')) {
      createDetails.open = true;
    }
    createDetails.addEventListener('toggle', () => recordPanel(project.id, 'create', createDetails.open));
    const createSummary = document.createElement('summary');
    createSummary.textContent = '新建需求或任务';
    retainCreationDraft(requirementForm, project.id, 'requirement');
    retainCreationDraft(taskForm, project.id, 'task');
    const choices = el('div', 'creation-switch');
    choices.setAttribute('role', 'group');
    choices.setAttribute('aria-label', '选择新建内容');
    const creationState = panelState.get(project.id) || {};
    let kind = creationState.creationKind || 'task';
    const choicesMap = new Map();
    const selectKind = next => {
        kind = next;
        const state = panelState.get(project.id) || {};
        state.creationKind = kind;
        panelState.set(project.id, state);
        requirementForm.hidden = kind !== 'requirement';
        taskForm.hidden = kind !== 'task';
        for (const [key, button] of choicesMap) {
            button.setAttribute('aria-pressed', String(key === kind));
        }
    };
    for (const [key, label] of [['task', '创建任务'], ['requirement', '创建需求']]) {
        const button = el('button', 'secondary', label);
        button.type = 'button';
        button.dataset.createKind = key;
        button.addEventListener('click', () => selectKind(key));
        choicesMap.set(key, button);
        choices.append(button);
    }
    selectKind(kind);
    forms.append(choices, requirementForm, taskForm);
    forms.append(el('p', 'hint creation-draft-hint', '切换项目或刷新数据时保留未提交内容；重新加载页面或退出登录后清除。'));
    createDetails.append(createSummary, forms);
    card.append(createDetails);
  }
    const state = filters.get(project.id) || { search: '', assignee: '', scope: 'all', view: 'board', sort: 'default' };
    filters.set(project.id, state);
    const requirementMap = new Map(requirements.map(item => [item.id, item]));
    if (!tasks.length) {
        const empty = el('div', 'empty-state compact board-empty', canManage
            ? '当前项目还没有任务。展开“新建需求或任务”保存第一条任务，之后可按状态在看板中推进。'
            : '当前项目还没有任务。任务由项目管理者创建，创建后会在这里按状态分列显示。');
        card.append(empty);
        return;
    }
    const controls = el('section', 'task-controls');
    controls.setAttribute('aria-label', '任务筛选与视图');
    const toolbar = el('div', 'board-toolbar');
    const search = el('input');
    search.type = 'search';
    search.placeholder = '搜索任务标题或关联需求';
    search.setAttribute('aria-label', '搜索任务');
    search.value = state.search;
    const assigneeFilter = el('select');
    assigneeFilter.setAttribute('aria-label', '按负责人筛选');
    assigneeFilter.append(option('', '所有负责人'), option('unassigned', '未指派'));
    members.forEach(member => assigneeFilter.append(option(member.user_id, member.email)));
    assigneeFilter.value = state.assignee;
    const sort = el('select');
    sort.setAttribute('aria-label', '任务排序');
    sort.append(option('default', '默认顺序'), option('due', '截止日优先'), option('newest', '最新创建'));
    sort.value = state.sort || 'default';
    toolbar.append(search, assigneeFilter, sort);
    const lower = el('div', 'task-control-lower');
    const scopes = el('div', 'task-scopes');
    scopes.setAttribute('role', 'group');
    scopes.setAttribute('aria-label', '快捷筛选任务');
    const scopeButtons = new Map();
    for (const [key, label] of Object.entries({ all: '全部', mine: '我负责', overdue: '已逾期', due_soon: '3天内到期', unassigned: '未指派' })) {
        const button = el('button', 'scope-button');
        button.type = 'button';
        button.dataset.scope = key;
        button.append(el('span', null, label), el('span', 'scope-count'));
        button.addEventListener('click', () => {
            state.scope = key;
            draw();
        });
        scopeButtons.set(key, button);
        scopes.append(button);
    }
    const views = el('div', 'task-view-switch');
    views.setAttribute('role', 'group');
    views.setAttribute('aria-label', '任务展示方式');
    const viewButtons = new Map();
    for (const [key, label] of Object.entries({ board: '看板视图', list: '列表视图' })) {
        const button = el('button', 'view-button', label);
        button.type = 'button';
        button.addEventListener('click', () => {
            state.view = key;
            draw();
        });
        viewButtons.set(key, button);
        views.append(button);
    }
    lower.append(scopes, views);
    controls.append(toolbar, lower);
    card.append(controls);
    const summary = el('div', 'task-results');
    const resultCount = el('p', 'hint');
    resultCount.setAttribute('role', 'status');
    const reset = el('button', 'reset-task-filters', '重置条件');
    reset.type = 'button';
    summary.append(resultCount, reset);
    card.append(summary);
    const boardHint = el('p', 'hint board-help', canManage
        ? '看板支持拖拽；列表适合逐项核对。快捷入口数量按项目全部任务计算，完成率不受筛选影响。'
        : '只读访问：可筛选、排序和切换视图，任务由项目管理者维护。');
    card.append(boardHint);
    const filterEmpty = el('div', 'filter-empty');
    filterEmpty.hidden = true;
    const filterEmptyText = el('p');
    const clearFilters = el('button', 'secondary', '清除筛选');
    clearFilters.type = 'button';
    filterEmpty.append(filterEmptyText, clearFilters);
    card.append(filterEmpty);
    const board = el('div', 'board');
    function taskCard(task) {
        const item = el('article', 'task-card');
        item.draggable = canManage && state.view !== 'list';
        item.addEventListener('dragstart', event => {
            event.dataTransfer?.setData('text/plain', String(task.id));
        });
        item.dataset.taskNumber = `任务 #${task.id}`;
        item.dataset.status = task.status;
        item.dataset.readonly = String(!canManage);
        item.append(el('strong', null, task.title));
        const meta = el('span', 'task-person');
        meta.append(el('span', 'task-assignee-name', task.assignee_email || '未指派'));
        meta.dataset.initial = task.assignee_email ? task.assignee_email.slice(0, 1).toUpperCase() : '未';
        item.append(meta);
        const requirement = requirementMap.get(task.requirement_id);
        if (requirement) {
            const tag = el('span', requirement.archived ? 'requirement-tag is-archived' : 'requirement-tag',
                requirement.archived ? `${requirement.title}（已归档）` : requirement.title);
            if (requirement.archived) {
                tag.title = '关联需求已归档：任务关联保留，恢复需求后可继续维护';
            }
            item.append(tag);
        } else {
            item.append(el('span', 'requirement-tag requirement-none', '未关联需求'));
        }
        if (task.due_date) {
            const overdue = task.due_date < localDay(new Date()) && task.status !== 'done';
            const due = el('span', overdue ? 'task-due is-overdue' : 'task-due', `${task.due_date}${overdue ? ' · 已逾期' : ''}`);
            meta.append(due);
        }
        item.append(el('span', 'task-status-badge', statusLabels[task.status]));
        if (canManage) {
            const actions = el('div', 'task-actions');
            const statusSelect = el('select');
            statusSelect.setAttribute('aria-label', `${task.title}的状态`);
            Object.entries(statusLabels).forEach(([key, text]) => statusSelect.append(option(key, text)));
            statusSelect.value = task.status;
            statusSelect.addEventListener('change', async () => {
                statusSelect.disabled = true;
                try {
                    await api(`/api/projects/${project.id}/tasks/${task.id}`, {
                        method: 'PUT', body: JSON.stringify({ status: statusSelect.value, version: task.version }),
                    });
                    say('任务状态已更新');
                    await renderProjects();
                } catch (error) {
                    statusSelect.value = task.status;
                    say(error.message, true);
                } finally {
                    statusSelect.disabled = false;
                }
            });
            const edit = el('button', 'secondary', '编辑');
            edit.type = 'button';
            edit.addEventListener('click', () => openTaskEditor(project, task, members, requirements));
            actions.append(statusSelect, edit);
            item.append(actions);
        }
        return item;
    }
    const draw = () => {
        board.replaceChildren();
        board.classList.toggle('task-list', state.view === 'list');
        const window = taskWindow();
        const visible = selectTaskView(tasks, requirementMap, state, current?.id, window);
        const filtering = Boolean(state.search || state.assignee || (state.scope && state.scope !== 'all'));
        for (const [key, button] of scopeButtons) {
            button.setAttribute('aria-pressed', String((state.scope || 'all') === key));
            button.querySelector('.scope-count').textContent = tasks.filter(task => matchesTaskScope(task, key, current?.id, window)).length;
        }
        for (const [key, button] of viewButtons) {
            button.setAttribute('aria-pressed', String((state.view || 'board') === key));
        }
        resultCount.textContent = `显示 ${visible.length} / ${tasks.length} 项任务`;
        reset.hidden = !filtering && (!state.sort || state.sort === 'default');
        filterEmptyText.textContent = `没有符合当前筛选条件的任务（本项目共 ${tasks.length} 项）。`;
        filterEmpty.hidden = !(filtering && !visible.length);
        if (state.view === 'list') {
            const headings = el('div', 'task-list-heading');
            headings.setAttribute('aria-hidden', 'true');
            for (const title of ['任务', '负责人 / 截止日', '关联需求', canManage ? '操作' : '状态']) {
                headings.append(el('span', null, title));
            }
            board.append(headings);
            for (const task of visible) {
                board.append(taskCard(task));
            }
            return;
        }
        for (const [status, label] of Object.entries(statusLabels)) {
            const column = el('section', 'board-column');
            column.dataset.status = status;
            const items = visible.filter(task => task.status === status);
            const title = el('h5', null, label);
            title.append(el('span', 'column-count', String(items.length)));
            column.append(title);
            if (canManage) {
                column.addEventListener('dragover', event => event.preventDefault());
                column.addEventListener('drop', async event => {
                    event.preventDefault();
                    const taskId = event.dataTransfer.getData('text/plain');
                    const task = tasks.find(item => String(item.id) === taskId);
                    if (!task || task.status === status) {
                        return;
                    }
                    try {
                        await api(`/api/projects/${project.id}/tasks/${task.id}`, { method: 'PUT', body: JSON.stringify({ status, version: task.version }) });
                        say('任务状态已更新');
                        await refresh();
                    } catch (error) {
                        say(error.message, true);
                    }
                });
            }
            items.forEach(task => column.append(taskCard(task)));
            if (!items.length) {
                column.append(el('p', 'column-empty', filtering ? '无匹配任务' : '暂无任务'));
            }
            board.append(column);
        }
    };
    const clear = () => {
        state.search = '';
        state.assignee = '';
        state.scope = 'all';
        state.sort = 'default';
        search.value = '';
        assigneeFilter.value = '';
        sort.value = 'default';
        draw();
        search.focus();
    };
    search.addEventListener('input', () => {
        state.search = search.value.trim();
        draw();
    });
    assigneeFilter.addEventListener('change', () => {
        state.assignee = assigneeFilter.value;
        draw();
    });
    sort.addEventListener('change', () => {
        state.sort = sort.value;
        draw();
    });
    clearFilters.addEventListener('click', clear);
    reset.addEventListener('click', clear);
    draw();
    card.append(board);
}
/* ---------- 弹窗：焦点管理、冲突锁与统一提示 ---------- */
function openDialog(id, focusName) {
  dialogOpener = document.activeElement;
  const dialog = $(id);
  dialogSnapshots.set(dialog.querySelector('form'), JSON.stringify(formData(dialog.querySelector('form'))));
  if (!dialog.open) dialog.showModal();
  const field = focusName ? dialog.querySelector(`[name="${focusName}"]`) : null;
  // 焦点落在标题输入框而不是“关闭”，键盘用户可直接编辑
  if (field) requestAnimationFrame(() => { field.focus(); if (field.select) field.select(); });
}
function closeDialog(id) {
  const dialog = $(id);
  if (id === 'taskDialog') {
    editingTask = null;
  } else if (id === 'requirementDialog') {
    editingRequirement = null;
  }
  if (dialog.open) {
    dialog.close();
  }
}
function requestDialogClose(id) {
    const form = $(id).querySelector('form');
    const changed = JSON.stringify(formData(form)) !== dialogSnapshots.get(form);
    if (changed && !confirm('有尚未保存的修改。确定放弃并关闭吗？')) {
        return;
    }
    closeDialog(id);
}
/* Esc 与按钮关闭都经由此处归还焦点，键盘用户不会丢位置 */
for (const id of ['taskDialog', 'requirementDialog']) {
  $(id).addEventListener('cancel', event => {
    event.preventDefault();
    requestDialogClose(id);
  });
  $(id).addEventListener('close', () => {
    if ($(id).open) {
      return;
    }
    if (id === 'taskDialog') {
      editingTask = null;
    } else {
      editingRequirement = null;
    }
    if (dialogOpener && dialogOpener.isConnected) dialogOpener.focus();
    dialogOpener = null;
  });
}
function resetDialog(formId, errorId) {
  const form = $(formId);
  delete form.dataset.conflict;
  const refreshButton = form.querySelector('[data-dialog-refresh]');
  if (refreshButton) refreshButton.hidden = true;
  // 上一轮冲突可能留下禁用的提交按钮，重新打开时要恢复可提交
  const submitButton = form.querySelector('[type="submit"]');
  if (submitButton) {
    submitButton.disabled = false;
    submitButton.textContent = '保存修改';
  }
  const error = $(errorId);
  error.textContent = '';
}
/* 409 冲突与 428 缺版本都不允许用旧数据覆盖：锁定提交、保留输入、给出刷新入口 */
function showDialogProblem(form, errorId, error) {
  const box = $(errorId);
  box.textContent = error.message;
  if (error.status === 409 || error.status === 428) {
    form.dataset.conflict = 'true';
    const refreshButton = form.querySelector('[data-dialog-refresh]');
    if (refreshButton) refreshButton.hidden = false;
  }
  box.focus();
}
document.querySelectorAll('[data-dialog-refresh]').forEach(button => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await refresh();
      button.closest('dialog').close();
      if (current) {
        say('已获取最新数据，请重新选择要编辑的条目');
      }
    } catch (error) {
      say(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
});
function openRequirementEditor(project, requirement) {
  editingRequirement = { project, requirement };
  const form = $('editRequirementForm');
  resetDialog('editRequirementForm', 'requirementDialogError');
  form.elements.title.value = requirement.title;
  form.elements.description.value = requirement.description || '';
  openDialog('requirementDialog', 'title');
}
$('closeRequirementDialog').addEventListener('click', () => requestDialogClose('requirementDialog'));
$('editRequirementForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingRequirement) return;
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  if (form.dataset.conflict === 'true' || button.disabled) {
    return;
  }
  const context = editingRequirement;
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    const data = formData(form);
    data.version = context.requirement.version;
    await api(`/api/projects/${context.project.id}/requirements/${context.requirement.id}`, { method: 'PUT', body: JSON.stringify(data) });
    if (editingRequirement !== context) {
      return;
    }
    closeDialog('requirementDialog');
    say('需求修改已保存');
    await renderProjects();
  } catch (error) {
    if (editingRequirement === context) {
      showDialogProblem(form, 'requirementDialogError', error);
    }
  } finally {
    if (editingRequirement === context) {
      button.disabled = form.dataset.conflict === 'true';
      button.textContent = '保存修改';
    }
  }
});
function openTaskEditor(project, task, members, requirements) {
  editingTask = { project, task };
  const form = $('editTaskForm');
  resetDialog('editTaskForm', 'taskDialogError');
  form.elements.title.value = task.title;
  // 原关联若已归档仍保留并标注，其余归档需求不出现在选项里
  form.elements.requirement_id.replaceChildren(option('', '不关联'), ...requirements
    .filter(item => !item.archived || item.id === task.requirement_id)
    .map(item => option(item.id, item.archived ? `${item.title}（已归档）` : item.title)));
  form.elements.requirement_id.value = task.requirement_id || '';
  form.elements.assignee_user_id.replaceChildren(option('', '暂不指派'), ...members.map(member => option(member.user_id, member.email)));
  form.elements.assignee_user_id.value = task.assignee_user_id || '';
  form.elements.due_date.value = task.due_date || '';
  form.elements.status.replaceChildren(...Object.entries(statusLabels).map(([key, label]) => option(key, label)));
  form.elements.status.value = task.status;
  openDialog('taskDialog', 'title');
}
$('closeTaskDialog').addEventListener('click', () => requestDialogClose('taskDialog'));
$('editTaskForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingTask) return;
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  if (form.dataset.conflict === 'true' || button.disabled) {
    return;
  }
  const context = editingTask;
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    await api(`/api/projects/${context.project.id}/tasks/${context.task.id}`, { method: 'PUT', body: JSON.stringify({ ...formData(form), version: context.task.version }) });
    if (editingTask !== context) {
      return;
    }
    closeDialog('taskDialog');
    say('任务修改已保存');
    await renderProjects();
  } catch (error) {
    if (editingTask === context) {
      showDialogProblem(form, 'taskDialogError', error);
    }
  } finally {
    if (editingTask === context) {
      button.disabled = form.dataset.conflict === 'true';
      button.textContent = '保存修改';
    }
  }
});
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
  setView(button.dataset.view);
  if (activeView === 'insights') renderInsights().catch(error => say(error.message, true));
}));
$('navToggle').addEventListener('click', () => {
    const button = $('navToggle');
    const expanded = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = expanded ? '收起导航' : '展开导航';
});
$('projectPicker').addEventListener('change', () => {
  activeProject = $('projectPicker').value;
  closeDialogs();
  renderCheckProjects();
  renderProjects().catch(error => say(error.message, true));
  if (activeView === 'insights') renderInsights().catch(error => say(error.message, true));
});
$('refreshWorkspace').addEventListener('click', async () => {
  $('refreshWorkspace').disabled = true;
  try {
    await refresh();
    if (current) {
      say('已获取最新数据');
    }
  } catch (error) {
    say(error.message, true);
  } finally {
    $('refreshWorkspace').disabled = false;
  }
});
function renderCheckProjects() {
  permissionCheckRequest += 1;
  const select = $('checkProject');
  if (projects.length) {
    select.replaceChildren(...projects.map(p => option(p.id, p.name)));
  } else {
    select.replaceChildren(option('', '暂无项目'));
  }
  select.disabled = !projects.length;
  $('checkView').disabled = !projects.length;
  $('checkManage').disabled = !projects.length;
  if (projects.length) {
    select.value = activeProject;
  }
  $('checkResult').textContent = '';
  if (!projects.length) $('checkResult').textContent = '当前没有可检查的项目。请先在“管理配置”中创建项目并加入成员。';
}
$('setupForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/setup', refresh); });
$('loginForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/login', refresh); });
$('roleForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/roles', refresh); });
$('userForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/users', refresh); });
$('projectForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/projects', refresh); });
$('logout').addEventListener('click', async () => {
  try {
    await api('/api/logout', { method: 'POST', body: '{}' });
    enterLoggedOut(false);
  } catch (error) {
    say(error.message, true);
  }
});
async function check(path) {
    const request = ++permissionCheckRequest;
    const account = current;
    const project = $('checkProject').value;
    const valid = () => request === permissionCheckRequest && account === current
        && project === $('checkProject').value;
    $('checkResult').textContent = '正在验证当前项目权限…';
    try {
        const data = await api(path);
        if (valid()) {
            $('checkResult').textContent = JSON.stringify(data, null, 2);
        }
    } catch (error) {
        if (valid()) {
            $('checkResult').textContent = error.message;
        }
    }
}
$('checkProject').addEventListener('change', () => {
    permissionCheckRequest += 1;
    $('checkResult').textContent = '';
});
$('checkView').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/members`));
$('checkManage').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/manage-check`));
refresh().catch(e => say(e.message, true));

/* ---------- 数据概览：健康度卡片、分布图、趋势、风险与变更记录 ---------- */
const SVG_NS = 'http://www.w3.org/2000/svg';
const auditFieldLabels = {
  status: '状态', title: '标题', requirement_id: '关联需求', assignee_user_id: '负责人',
  due_date: '截止日', position: '排序', description: '说明', role_id: '角色', archived: '归档状态',
};
const auditActionLabels = { create: '新增', update: '修改', archive: '归档', restore: '恢复', remove: '移出' };
const auditEntityLabels = { task: '任务', requirement: '需求', membership: '成员' };
let insightRequest = 0;
let insightDrillRequest = 0;
let insightAuditType = 'all';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function svgNode(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}
function insightEmpty(text) { return el('div', 'empty-state compact', text); }
function chartScroll(chart) {
  const wrap = el('div', 'chart-scroll');
  wrap.append(chart);
  return wrap;
}
function shortName(text, limit = 18) {
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}
function activateOn(button, handler) {
  button.addEventListener('click', handler);
  if (button.tagName.toLowerCase() !== 'button') {
    button.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        handler();
      }
    });
  }
}
async function drillTasks(target) {
    const panel = $('insightDrill');
    if (!panel || !current || !activeProject) {
        return;
    }
    const request = ++insightDrillRequest;
    const project = activeProject;
    const valid = () => request === insightDrillRequest && current && project === activeProject
        && panel.isConnected && activeView === 'insights';
    panel.setAttribute('aria-busy', 'true');
    try {
        const query = new URLSearchParams({ dimension: target.dimension, value: target.value || '', limit: '200' });
        const data = await api(`/api/projects/${project}/stats/tasks?${query}`);
        if (!valid()) {
            return;
        }
        panel.replaceChildren(el('h4', null, `${target.label} · ${data.total} 项`));
        panel.append(data.items.length ? taskTable(data.items) : insightEmpty('该维度下没有任务。'));
        if (data.truncated) {
            panel.append(el('p', 'hint', `共 ${data.total} 项，当前显示 ${data.items.length} 项。更多任务请回到看板筛选查看。`));
        }
        panel.focus();
    } catch (error) {
        if (valid()) {
            panel.replaceChildren(el('p', 'error', `无法加载任务：${error.message}`));
        }
    } finally {
        if (valid()) {
            panel.removeAttribute('aria-busy');
        }
    }
}
async function renderInsights() {
  const body = $('insightBody');
  const scope = $('insightScope');
  const request = ++insightRequest;
  insightDrillRequest += 1;
  if (!activeProject) {
    scope.textContent = '当前项目：—';
    body.replaceChildren(insightEmpty('当前账号还没有可访问的项目。创建项目并加入成员后，这里会显示任务总数、完成率与完成趋势。'));
    body.removeAttribute('aria-busy');
    return;
  }
  const project = projects.find(item => String(item.id) === activeProject);
  scope.textContent = `当前项目：${project ? project.name : '—'}`;
  body.setAttribute('aria-busy', 'true');
  body.replaceChildren(el('p', 'insight-loading', '正在加载当前项目的数据…'));
  try {
    const base = `/api/projects/${activeProject}`;
    const filter = insightAuditType === 'all' ? '' : `entity_type=${insightAuditType}&`;
    const [stats, risks, audit] = await Promise.all([
      api(`${base}/stats`),
      api(`${base}/risks?limit=10`),
      api(`${base}/audit?${filter}limit=12`),
    ]);
    if (request !== insightRequest) return;
    body.replaceChildren(kpiBlock(stats), statusBlock(stats), trendBlock(stats),
      assigneeBlock(stats), requirementBlock(stats), riskBlock(risks), auditBlock(audit));
  } catch (error) {
    if (request === insightRequest) {
      const problem = insightEmpty(`无法加载统计数据：${error.message}`);
      const retry = el('button', 'secondary', '重试加载统计');
      retry.type = 'button';
      retry.addEventListener('click', () => renderInsights().catch(cause => say(cause.message, true)));
      problem.append(retry);
      body.replaceChildren(problem);
    }
  } finally {
    if (request === insightRequest) body.removeAttribute('aria-busy');
  }
}
function kpiBlock(stats) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '项目健康度'));
  card.append(el('p', 'hint', `统计基准日 ${stats.as_of}；逾期按“未完成且截止日早于基准日”判定。`));
  const grid = el('div', 'kpi-grid');
  const plain = el('div', 'kpi');
  plain.append(el('span', 'kpi-label', '任务总数'), el('strong', 'kpi-value', String(stats.total)),
    el('span', 'kpi-note', '当前项目全部任务'));
  grid.append(plain);
  grid.append(kpiButton('已完成', String(stats.completed), `完成率 ${stats.completion_rate}%`,
    { dimension: 'status', value: 'done', label: '已完成任务' }));
  grid.append(kpiButton('已逾期', String(stats.overdue), '未完成且已过截止日',
    { dimension: 'overdue', value: '', label: '已逾期任务' }));
  grid.append(kpiButton('3 天内到期', String(stats.due_soon), '未完成且临期',
    { dimension: 'due_soon', value: '', label: '临期任务' }));
  card.append(grid);
  const drill = el('section', 'insight-drill');
  drill.id = 'insightDrill';
  drill.tabIndex = -1;
  drill.append(el('p', 'hint', '点击卡片、柱形、数据点或“下钻”按钮，在这里核对对应的任务明细。'));
  card.append(drill);
  return card;
}
function kpiButton(title, value, note, target) {
  const node = el('button', 'kpi');
  node.type = 'button';
  node.setAttribute('aria-label', `${title} ${value}，查看任务明细`);
  node.append(el('span', 'kpi-label', title), el('strong', 'kpi-value', value), el('span', 'kpi-note', note));
  activateOn(node, () => drillTasks(target));
  return node;
}
function statusBlock(stats) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '状态分布'));
  card.append(el('p', 'hint', '柱形高度表示任务数量；点击柱形或按回车查看该状态的任务明细。'));
  const chart = svgNode('svg', { viewBox: '0 0 480 210', class: 'chart', role: 'img',
    'aria-label': `状态分布：${stats.by_status.map(item => `${statusLabels[item.status]} ${item.count} 项`).join('，')}` });
  const max = Math.max(1, ...stats.by_status.map(item => item.count));
  const slot = 480 / stats.by_status.length;
  const width = 64;
  stats.by_status.forEach((item, index) => {
    const height = Math.round(item.count / max * 120) + 2;
    const x = Math.round(index * slot + (slot - width) / 2);
    const y = 160 - height;
    const bar = svgNode('rect', { x, y, width, height, rx: 8, class: 'chart-bar',
      tabindex: '0', role: 'button',
      'aria-label': `${statusLabels[item.status]} ${item.count} 项，查看任务明细` });
    activateOn(bar, () => drillTasks({ dimension: 'status', value: item.status, label: statusLabels[item.status] }));
    chart.append(bar);
    const value = svgNode('text', { x: x + width / 2, y: y - 10, class: 'chart-value', 'text-anchor': 'middle' });
    value.textContent = String(item.count);
    const label = svgNode('text', { x: x + width / 2, y: 184, class: 'chart-label', 'text-anchor': 'middle' });
    label.textContent = statusLabels[item.status];
    chart.append(value, label);
  });
  chart.append(svgNode('line', { x1: 20, y1: 160, x2: 460, y2: 160, class: 'chart-axis' }));
  card.append(chartScroll(chart));
  return card;
}
function trendBlock(stats) {
  const card = el('section', 'insight-card');
  const trend = stats.trend;
  const first = trend[0];
  const last = trend[trend.length - 1];
  card.append(el('h3', null, `完成趋势（最近 ${stats.window_days} 天）`));
  card.append(el('p', 'hint', '按任务首次进入“已完成”的日期累计；历史完成且没有事件记录的任务计入窗口起点。点击数据点查看当天完成的任务。'));
  const max = Math.max(1, ...trend.map(item => item.completed));
  const left = 40, right = 456, top = 24, bottom = 150;
  const step = (right - left) / Math.max(1, trend.length - 1);
  const points = trend.map((item, index) => ({
    x: left + index * step,
    y: bottom - (item.completed / max) * (bottom - top),
    ...item,
  }));
  const chart = svgNode('svg', { viewBox: '0 0 480 200', class: 'chart', role: 'img',
    'aria-label': `完成趋势：${first.date} 累计 ${first.completed} 项，${last.date} 累计 ${last.completed} 项` });
  chart.append(svgNode('line', { x1: left, y1: bottom, x2: right, y2: bottom, class: 'chart-axis' }));
  chart.append(svgNode('line', { x1: left, y1: top, x2: left, y2: bottom, class: 'chart-axis' }));
  chart.append(svgNode('polyline', {
    points: points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '),
    class: 'chart-line',
  }));
  points.forEach(point => {
    const dot = svgNode('circle', { cx: point.x, cy: point.y, r: 5, class: 'chart-dot',
      tabindex: '0', role: 'button',
      'aria-label': `${point.date} 累计完成 ${point.completed} 项，查看当天完成的任务` });
    activateOn(dot, () => drillTasks({
      dimension: 'completed_on', value: point.date, label: `${point.date} 完成的任务`,
    }));
    chart.append(dot);
  });
  [0, Math.floor((trend.length - 1) / 2), trend.length - 1].forEach(index => {
    const point = points[index];
    const label = svgNode('text', { x: point.x, y: 176, class: 'chart-label',
      'text-anchor': index === 0 ? 'start' : (index === trend.length - 1 ? 'end' : 'middle') });
    label.textContent = point.date.slice(5);
    chart.append(label);
  });
  const axisValue = svgNode('text', { x: 8, y: top + 4, class: 'chart-label' });
  axisValue.textContent = String(max);
  const axisZero = svgNode('text', { x: 8, y: bottom + 4, class: 'chart-label' });
  axisZero.textContent = '0';
  chart.append(axisValue, axisZero);
  card.append(chartScroll(chart));
  return card;
}
function assigneeBlock(stats) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '负责人分布'));
  card.append(el('p', 'hint', '横条长度为任务数，深色段为已完成；点击横条查看该负责人的任务明细。'));
  const items = stats.by_assignee.slice(0, 6);
  if (!items.length) {
    card.append(insightEmpty('还没有任务，无法计算负责人分布。'));
    return card;
  }
  const max = Math.max(1, ...items.map(item => item.total));
  const row = 30, left = 132, width = 300;
  const chart = svgNode('svg', { viewBox: `0 0 ${480} ${items.length * row + 10}`, class: 'chart', role: 'img',
    'aria-label': `负责人分布：${items.map(item => `${item.email} ${item.total} 项`).join('，')}` });
  items.forEach((item, index) => {
    const y = index * row + 6;
    const total = Math.round(item.total / max * width);
    const done = item.total ? Math.round(item.completed / item.total * total) : 0;
    const bar = svgNode('rect', { x: left, y, width: Math.max(2, total), height: 18, rx: 6,
      class: 'chart-bar', tabindex: '0', role: 'button',
      'aria-label': `${item.email} ${item.total} 项，已完成 ${item.completed} 项，查看明细` });
    activateOn(bar, () => drillTasks({
      dimension: 'assignee', value: item.user_id === null ? 'unassigned' : String(item.user_id),
      label: `${item.email} 的任务`,
    }));
    const filled = svgNode('rect', { x: left, y, width: done, height: 18, rx: 6, class: 'chart-bar-done' });
    const name = svgNode('text', { x: 8, y: y + 14, class: 'chart-label' });
    name.textContent = shortName(item.email);
    const value = svgNode('text', { x: left + total + 8, y: y + 14, class: 'chart-label' });
    value.textContent = `${item.total}／${item.completed}`;
    chart.append(bar, filled, name, value);
  });
  card.append(chartScroll(chart));
  return card;
}
function requirementBlock(stats) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '需求完成率'));
  card.append(el('p', 'hint', '只统计已经挂接任务的需求；归档需求的历史任务仍计入。'));
  if (!stats.by_requirement.length) {
    card.append(insightEmpty('还没有关联任务的需求。'));
    return card;
  }
  const table = el('table', 'mini-table');
  const head = el('thead');
  const headRow = el('tr');
  ['需求', '任务数', '已完成', '完成率', '操作'].forEach((text, index) => {
    const th = el('th', null, text);
    th.scope = 'col';
    if (index >= 1 && index <= 3) th.className = 'numeric';
    headRow.append(th);
  });
  head.append(headRow);
  const body = el('tbody');
  for (const item of stats.by_requirement) {
    const row = el('tr');
    const title = el('td', null, item.archived ? `${item.title}（已归档）` : item.title);
    const total = el('td', 'numeric', String(item.total));
    const done = el('td', 'numeric', String(item.completed));
    const rate = el('td', 'numeric', `${item.total ? Math.round(item.completed * 1000 / item.total) / 10 : 0}%`);
    const action = el('td');
    const button = el('button', 'secondary small', '下钻');
    button.type = 'button';
    button.addEventListener('click', () => drillTasks({
      dimension: 'requirement', value: item.requirement_id === null ? 'none' : String(item.requirement_id),
      label: `${item.title} 的任务`,
    }));
    action.append(button);
    row.append(title, total, done, rate, action);
    body.append(row);
  }
  table.append(head, body);
  const wrap = el('div', 'table-scroll');
  wrap.append(table);
  card.append(wrap);
  return card;
}
function riskBlock(risks) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '逾期与风险任务'));
  card.append(el('p', 'hint', `基准日 ${risks.as_of}；列出已逾期与 3 天内到期且未完成的任务，最多 10 条。`));
  if (!risks.items.length) {
    card.append(insightEmpty('当前没有逾期或临期任务。'));
    return card;
  }
  const table = el('table', 'mini-table');
  const head = el('thead');
  const headRow = el('tr');
  ['任务', '负责人', '截止日', '状态', '剩余'].forEach(text => {
    const th = el('th', null, text);
    th.scope = 'col';
    headRow.append(th);
  });
  head.append(headRow);
  const body = el('tbody');
  for (const item of risks.items) {
    const row = el('tr');
    const days = item.days_left < 0 ? `逾期 ${-item.days_left} 天` : `${item.days_left} 天`;
    const level = el('td', null, days);
    level.className = item.level === 'overdue' ? 'risk-overdue' : 'risk-soon';
    row.append(el('td', null, item.title), el('td', null, item.assignee_email || '未指派'),
      el('td', null, item.due_date), el('td', null, statusLabels[item.status]), level);
    body.append(row);
  }
  table.append(head, body);
  const wrap = el('div', 'table-scroll');
  wrap.append(table);
  card.append(wrap);
  return card;
}
function auditBlock(events) {
  const card = el('section', 'insight-card');
  card.append(el('h3', null, '变更记录'));
  card.append(el('p', 'hint', '追溯任务、需求与成员角色的最近变更。'));
  const select = el('select');
  select.setAttribute('aria-label', '按对象类型筛选变更记录');
  [['all', '全部类型'], ['task', '任务'], ['requirement', '需求'], ['membership', '成员角色']]
    .forEach(([value, text]) => select.append(option(value, text)));
  select.value = insightAuditType;
  select.addEventListener('change', () => {
    insightAuditType = select.value;
    renderInsights().catch(error => say(error.message, true));
  });
  card.append(select);
  if (!events.length) {
    card.append(insightEmpty('当前筛选条件下没有变更记录。'));
    return card;
  }
  const list = el('ol', 'audit-list');
  for (const event of events) {
    const item = el('li');
    const when = el('time', 'audit-time', event.created_at.slice(5, 16));
    const who = event.actor_email || '系统';
    const entity = auditEntityLabels[event.entity_type] || event.entity_type;
    const label = event.entity_label ? `「${event.entity_label}」` : `#${event.entity_id}`;
    let text = `${who} ${auditActionLabels[event.action] || event.action}${entity}${label}`;
    if (event.field) {
      const field = auditFieldLabels[event.field] || event.field;
      const format = value => {
        if (value === null) {
          return '空';
        }
        return event.field === 'status' ? (statusLabels[value] || value) : value;
      };
      const before = format(event.from_value);
      const after = format(event.to_value);
      text += ` · ${field} ${before} → ${after}`;
    }
    item.append(when, document.createTextNode(text));
    list.append(item);
  }
  card.append(list);
  return card;
}
function taskTable(items) {
  const table = el('table', 'mini-table');
  const head = el('thead');
  const headRow = el('tr');
  ['任务', '状态', '负责人', '截止日'].forEach(text => {
    const th = el('th', null, text);
    th.scope = 'col';
    headRow.append(th);
  });
  head.append(headRow);
  const body = el('tbody');
  for (const item of items) {
    const row = el('tr');
    row.append(el('td', null, item.title), el('td', null, statusLabels[item.status] || item.status),
      el('td', null, item.assignee_email || '未指派'), el('td', null, item.due_date || '—'));
    body.append(row);
  }
  table.append(head, body);
  const wrap = el('div', 'table-scroll');
  wrap.append(table);
  return wrap;
}
