/* 页面展示与请求。所有真正的权限判断由服务器完成。 */
const $ = id => document.getElementById(id);
let current = null, roles = [], roleOptions = [], users = [], projects = [];
const permissionLabels = { 'project.view': '查看项目', 'members.manage': '管理成员', 'projects.manage': '管理项目', 'roles.manage': '管理角色', 'workitems.manage': '管理需求任务' };
const statusLabels = { backlog: '待梳理', todo: '待办', doing: '进行中', done: '已完成' };
let activeProject = '', activeView = 'collaboration', projectRequest = 0, editingTask = null;
const filters = new Map();
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...options });
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
    if (response.status === 401 && current) {
      current = null;
      $('workspace').hidden = true;
      $('logout').hidden = true;
      $('loginPanel').hidden = false;
      $('taskDialog').close();
    }
    const error = new Error(`${response.status} ${data.error || '请求失败'}`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function say(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : 'ok'; }
function formData(form) { return Object.fromEntries(new FormData(form).entries()); }
async function submit(form, path, after) {
  const buttons = [...form.querySelectorAll('button')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const created = await api(path, { method: 'POST', body: JSON.stringify(formData(form)) });
    if (path === '/api/projects') {
      activeProject = String(created.id);
      activeView = 'collaboration';
    }
    form.reset();
    say('保存成功，数据已更新');
    await after();
  } catch (error) {
    say(error.message, true);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}
function cell(text) { const td = document.createElement('td'); td.textContent = text; return td; }
function option(value, text) { const item = document.createElement('option'); item.value = value; item.textContent = text; return item; }
async function refresh() {
  try {
    current = await api('/api/me');
  } catch (error) {
    if (error.status !== 401) {
      throw error;
    }
    current = null;
  }
  $('setupPanel').hidden = true; $('loginPanel').hidden = true; $('workspace').hidden = !current; $('logout').hidden = !current;
  if (!current) { const status = await api('/api/status'); $(status.needs_setup ? 'setupPanel' : 'loginPanel').hidden = false; return; }
  $('currentUser').textContent = current.email;
  const canManageRoles = current.can_manage_roles;
  const canManageProjects = current.can_manage_projects;
  $('adminPanel').hidden = !(current.is_admin || canManageRoles || canManageProjects);
  $('roleForm').hidden = !canManageRoles; $('matrixPanel').hidden = !canManageRoles;
  $('projectForm').hidden = !canManageProjects; $('userForm').hidden = !current.is_admin;
  roleOptions = await api('/api/role-options');
  roles = canManageRoles ? await api('/api/roles') : [];
  projects = await api('/api/projects');
  users = current.is_admin ? await api('/api/users') : (current.can_manage_members ? await api('/api/user-options') : []);
  $('roleCount').textContent = roleOptions.length; $('projectCount').textContent = projects.length;
  if (!projects.some(project => String(project.id) === activeProject)) {
    activeProject = projects.length ? String(projects[0].id) : '';
  }
  $('projectPicker').replaceChildren(...projects.map(project => option(project.id, project.name)));
  $('projectPicker').disabled = !projects.length;
  $('projectPicker').value = activeProject;
  renderMatrix();
  setView(activeView);
  await renderProjects();
  renderCheckProjects();
}
function setView(view) {
  const canAdmin = current && (current.is_admin || current.can_manage_roles || current.can_manage_projects);
  activeView = view === 'administration' && !canAdmin ? 'collaboration' : view;
  $('collaborationPanel').hidden = activeView !== 'collaboration';
  $('adminPanel').hidden = activeView !== 'administration' || !canAdmin;
  $('checkPanel').hidden = activeView !== 'verification';
  document.querySelectorAll('[data-view]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === activeView));
    button.hidden = button.dataset.view === 'administration' && !canAdmin;
  });
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
    const action = document.createElement('td'), button = document.createElement('button'); button.textContent = '保存权限';
    button.addEventListener('click', async () => { try { await api(`/api/roles/${role.id}/permissions`, { method: 'PUT', body: JSON.stringify({ permissions: Object.keys(checks).filter(k => checks[k].checked) }) }); say('权限已保存'); await refresh(); } catch (e) { say(e.message, true); } });
    action.append(button); tr.append(action); body.append(tr);
  }
}
async function renderProjects() {
  const request = ++projectRequest;
  const target = $('projects'); target.replaceChildren();
  if (!projects.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = '当前没有可访问项目。管理员可在管理配置中创建项目并添加成员；普通成员请联系项目管理者。';
    target.append(empty);
    return;
  }
  for (const project of projects.filter(item => String(item.id) === activeProject)) {
    const card = document.createElement('section'); card.className = 'project'; const title = document.createElement('h3'); title.textContent = project.name; card.append(title);
    target.append(card);
    try {
      const [members, requirements, tasks] = await Promise.all([
        api(`/api/projects/${project.id}/members`),
        api(`/api/projects/${project.id}/requirements`),
        api(`/api/projects/${project.id}/tasks`),
      ]);
      const canManageMembers = await allowed(`/api/projects/${project.id}/manage-check`);
      const canManageWorkitems = await allowed(`/api/projects/${project.id}/workitems/manage-check`);
      const assignableRoles = canManageMembers ? await api(`/api/projects/${project.id}/role-options`) : [];
      if (request !== projectRequest) {
        return;
      }
      renderMembers(card, project, members, canManageMembers, assignableRoles);
      renderWorkItems(card, project, members, requirements, tasks, canManageWorkitems);
    } catch (error) {
      const problem = document.createElement('p'); problem.className = 'error'; problem.textContent = error.message; card.append(problem);
    }
  }
}
function renderMembers(card, project, members, canManage, assignableRoles) {
  const details = document.createElement('details');
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
      remove.addEventListener('click', async () => { if (!confirm(`移出 ${member.email}？`)) return; try { await api(`/api/projects/${project.id}/members/${member.user_id}`, { method: 'DELETE' }); say('成员已移出，权限已回收'); await refresh(); } catch (e) { say(e.message, true); } });
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
  const requirementList = document.createElement('ul'); requirementList.className = 'requirements';
  if (!requirements.length) { const empty = document.createElement('li'); empty.textContent = '暂无需求条目'; requirementList.append(empty); }
  requirements.forEach(item => { const li = document.createElement('li'); const strong = document.createElement('strong'); strong.textContent = item.title; li.append(strong); if (item.description) li.append(document.createTextNode(` — ${item.description}`)); requirementList.append(li); });
  const requirementDetails = document.createElement('details');
  const requirementSummary = document.createElement('summary');
  requirementSummary.textContent = `需求列表 · ${requirements.length} 条`;
  requirementDetails.append(requirementSummary, requirementList);
  card.append(requirementDetails);
  if (canManage) {
    const forms = document.createElement('div'); forms.className = 'workitem-forms';
    const requirementForm = document.createElement('form'); requirementForm.innerHTML = '<h5>新增需求</h5><label>标题<input name="title" maxlength="80" required></label><label>说明<input name="description" maxlength="500"></label><button>保存需求</button>';
    requirementForm.addEventListener('submit', event => { event.preventDefault(); submit(requirementForm, `/api/projects/${project.id}/requirements`, refresh); });
    const taskForm = document.createElement('form'); const taskTitle = document.createElement('h5'); taskTitle.textContent = '新增任务'; taskForm.append(taskTitle);
    const titleLabel = document.createElement('label'); titleLabel.textContent = '标题'; const titleInput = document.createElement('input'); titleInput.name = 'title'; titleInput.maxLength = 100; titleInput.required = true; titleLabel.append(titleInput); taskForm.append(titleLabel);
    const requirementLabel = document.createElement('label'); requirementLabel.textContent = '关联需求'; const requirementSelect = document.createElement('select'); requirementSelect.name = 'requirement_id'; requirementSelect.append(option('', '不关联')); requirements.forEach(item => requirementSelect.append(option(item.id, item.title))); requirementLabel.append(requirementSelect); taskForm.append(requirementLabel);
    const assigneeLabel = document.createElement('label'); assigneeLabel.textContent = '负责人'; const assigneeSelect = document.createElement('select'); assigneeSelect.name = 'assignee_user_id'; assigneeSelect.append(option('', '暂不指派')); members.forEach(item => assigneeSelect.append(option(item.user_id, item.email))); assigneeLabel.append(assigneeSelect); taskForm.append(assigneeLabel);
    const dueLabel = document.createElement('label'); dueLabel.textContent = '截止日'; const dueInput = document.createElement('input'); dueInput.type = 'date'; dueInput.name = 'due_date'; dueLabel.append(dueInput); taskForm.append(dueLabel);
    const status = document.createElement('input'); status.type = 'hidden'; status.name = 'status'; status.value = 'todo'; taskForm.append(status);
    const create = document.createElement('button'); create.textContent = '保存任务'; taskForm.append(create);
    taskForm.addEventListener('submit', event => { event.preventDefault(); submit(taskForm, `/api/projects/${project.id}/tasks`, refresh); });
    const createDetails = document.createElement('details');
    const createSummary = document.createElement('summary');
    createSummary.textContent = '新建需求或任务';
    forms.append(requirementForm, taskForm);
    createDetails.append(createSummary, forms);
    card.append(createDetails);
  }
  const state = filters.get(project.id) || { search: '', assignee: '' };
  filters.set(project.id, state);
  const toolbar = document.createElement('div');
  toolbar.className = 'board-toolbar';
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = '搜索任务标题或关联需求';
  search.setAttribute('aria-label', '搜索任务');
  search.value = state.search;
  const assigneeFilter = document.createElement('select');
  assigneeFilter.setAttribute('aria-label', '按负责人筛选');
  assigneeFilter.append(option('', '所有负责人'), option('unassigned', '未指派'));
  members.forEach(member => assigneeFilter.append(option(member.user_id, member.email)));
  assigneeFilter.value = state.assignee;
  toolbar.append(search, assigneeFilter);
  card.append(toolbar);
  const board = document.createElement('div'); board.className = 'board';
  const draw = () => {
  board.replaceChildren();
  const visible = tasks.filter(task => {
    const requirement = requirements.find(item => item.id === task.requirement_id);
    return `${task.title} ${requirement?.title || ''}`.toLowerCase().includes(state.search.toLowerCase()) &&
      (!state.assignee || (state.assignee === 'unassigned' ? !task.assignee_user_id : String(task.assignee_user_id) === state.assignee));
  });
  for (const [status, label] of Object.entries(statusLabels)) {
    const column = document.createElement('section'); column.className = 'board-column'; column.dataset.status = status;
    const columnTitle = document.createElement('h5'); columnTitle.textContent = `${label} (${visible.filter(task => task.status === status).length})`; column.append(columnTitle);
    if (canManage) {
      column.addEventListener('dragover', event => event.preventDefault());
      column.addEventListener('drop', async event => {
        event.preventDefault();
        const taskId = event.dataTransfer.getData('text/plain');
        const task = tasks.find(item => String(item.id) === taskId);
        if (!task) {
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
    for (const task of visible.filter(item => item.status === status)) {
      const item = document.createElement('article'); item.className = 'task-card'; item.draggable = canManage; item.addEventListener('dragstart', event => event.dataTransfer.setData('text/plain', task.id));
      const name = document.createElement('strong'); name.textContent = task.title; item.append(name);
      const meta = document.createElement('span'); meta.textContent = `${task.assignee_email || '未指派'}${task.due_date ? ` · ${task.due_date}` : ''}`; item.append(meta); column.append(item);
      const requirement = requirements.find(entry => entry.id === task.requirement_id);
      if (requirement) {
        const tag = document.createElement('span');
        tag.className = 'requirement-tag';
        tag.textContent = requirement.title;
        item.append(tag);
      }
      const today = new Date();
      const localDate = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
      if (task.due_date && task.due_date < localDate && task.status !== 'done') {
        meta.classList.add('overdue');
        meta.textContent += ' · 已逾期';
      }
      if (canManage) {
        const actions = document.createElement('div');
        actions.className = 'task-actions';
        const statusSelect = document.createElement('select');
        statusSelect.setAttribute('aria-label', `${task.title}的状态`);
        Object.entries(statusLabels).forEach(([key, text]) => statusSelect.append(option(key, text)));
        statusSelect.value = task.status;
        statusSelect.addEventListener('change', async () => {
          statusSelect.disabled = true;
          try {
            await api(`/api/projects/${project.id}/tasks/${task.id}`, { method: 'PUT', body: JSON.stringify({ status: statusSelect.value, version: task.version }) });
            say('任务状态已更新');
            await renderProjects();
          } catch (error) {
            statusSelect.value = task.status;
            say(error.message, true);
          } finally {
            statusSelect.disabled = false;
          }
        });
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'secondary';
        edit.textContent = '编辑';
        edit.addEventListener('click', () => openTaskEditor(project, task, members, requirements));
        actions.append(statusSelect, edit);
        item.append(actions);
      }
    }
    if (!visible.some(task => task.status === status)) {
      const empty = document.createElement('p');
      empty.className = 'column-empty';
      empty.textContent = state.search || state.assignee ? '没有匹配的任务' : '暂无任务';
      column.append(empty);
    }
    board.append(column);
  }
  };
  search.addEventListener('input', () => { state.search = search.value.trim(); draw(); });
  assigneeFilter.addEventListener('change', () => { state.assignee = assigneeFilter.value; draw(); });
  draw();
  card.append(board);
}
function openTaskEditor(project, task, members, requirements) {
  editingTask = { project, task };
  const form = $('editTaskForm');
  form.elements.title.value = task.title;
  form.elements.requirement_id.replaceChildren(option('', '不关联'), ...requirements.map(item => option(item.id, item.title)));
  form.elements.requirement_id.value = task.requirement_id || '';
  form.elements.assignee_user_id.replaceChildren(option('', '暂不指派'), ...members.map(member => option(member.user_id, member.email)));
  form.elements.assignee_user_id.value = task.assignee_user_id || '';
  form.elements.due_date.value = task.due_date || '';
  form.elements.status.replaceChildren(...Object.entries(statusLabels).map(([key, label]) => option(key, label)));
  form.elements.status.value = task.status;
  $('taskDialogError').textContent = '';
  $('taskDialog').showModal();
}
$('closeTaskDialog').addEventListener('click', () => $('taskDialog').close());
$('editTaskForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingTask) {
    return;
  }
  const button = event.currentTarget.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await api(`/api/projects/${editingTask.project.id}/tasks/${editingTask.task.id}`, { method: 'PUT', body: JSON.stringify({ ...formData(event.currentTarget), version: editingTask.task.version }) });
    $('taskDialog').close();
    say('任务修改已保存');
    await renderProjects();
  } catch (error) {
    $('taskDialogError').textContent = error.status === 409 ? `${error.message} 当前输入尚未保存；请关闭弹窗后点击刷新数据。` : error.message;
  } finally {
    button.disabled = false;
  }
});
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
$('projectPicker').addEventListener('change', () => {
  activeProject = $('projectPicker').value;
  renderProjects().catch(error => say(error.message, true));
});
$('refreshWorkspace').addEventListener('click', async () => {
  $('refreshWorkspace').disabled = true;
  try {
    await refresh();
    say('已获取最新数据');
  } catch (error) {
    say(error.message, true);
  } finally {
    $('refreshWorkspace').disabled = false;
  }
});
function renderCheckProjects() { const select = $('checkProject'); select.replaceChildren(); projects.forEach(p => select.append(option(p.id, p.name))); }
$('setupForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/setup', refresh); });
$('loginForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/login', refresh); });
$('roleForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/roles', refresh); });
$('userForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/users', refresh); });
$('projectForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/projects', refresh); });
$('logout').addEventListener('click', async () => {
  try {
    await api('/api/logout', { method: 'POST', body: '{}' });
    filters.clear();
    activeProject = '';
    await refresh();
    say('已安全退出');
  } catch (error) {
    say(error.message, true);
  }
});
async function check(path) { try { const data = await api(path); $('checkResult').textContent = JSON.stringify(data, null, 2); } catch (e) { $('checkResult').textContent = e.message; } }
$('checkView').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/members`));
$('checkManage').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/manage-check`));
refresh().catch(e => say(e.message, true));
