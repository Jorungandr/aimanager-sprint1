/* 页面展示与请求。所有真正的权限判断由服务器完成。 */
const $ = id => document.getElementById(id);
let current = null, roles = [], roleOptions = [], users = [], projects = [];
const permissionLabels = { 'project.view': '查看项目', 'members.manage': '管理成员', 'projects.manage': '管理项目', 'roles.manage': '管理角色', 'workitems.manage': '管理需求任务' };
const statusLabels = { backlog: '待梳理', todo: '待办', doing: '进行中', done: '已完成' };
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(`${response.status} ${data.error || '请求失败'}`);
  return data;
}
function say(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : 'ok'; }
function formData(form) { return Object.fromEntries(new FormData(form).entries()); }
async function submit(form, path, after) {
  try { await api(path, { method: 'POST', body: JSON.stringify(formData(form)) }); form.reset(); say('操作成功'); await after(); }
  catch (error) { say(error.message, true); }
}
function cell(text) { const td = document.createElement('td'); td.textContent = text; return td; }
function option(value, text) { const item = document.createElement('option'); item.value = value; item.textContent = text; return item; }
async function refresh() {
  try { current = await api('/api/me'); } catch (_) { current = null; }
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
  renderMatrix(); await renderProjects(); renderCheckProjects();
}
function renderMatrix() {
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
  const target = $('projects'); target.replaceChildren();
  if (!projects.length) { const p = document.createElement('p'); p.textContent = '当前没有可访问项目。'; target.append(p); return; }
  for (const project of projects) {
    const card = document.createElement('section'); card.className = 'project'; const title = document.createElement('h3'); title.textContent = project.name; card.append(title);
    target.append(card);
    try {
      const [members, requirements, tasks] = await Promise.all([
        api(`/api/projects/${project.id}/members`),
        api(`/api/projects/${project.id}/requirements`),
        api(`/api/projects/${project.id}/tasks`),
      ]);
      const canManageMembers = await api(`/api/projects/${project.id}/manage-check`).then(() => true).catch(() => false);
      const canManageWorkitems = await api(`/api/projects/${project.id}/workitems/manage-check`).then(() => true).catch(() => false);
      renderMembers(card, project, members, canManageMembers);
      renderWorkItems(card, project, members, requirements, tasks, canManageWorkitems);
    } catch (error) {
      const problem = document.createElement('p'); problem.className = 'error'; problem.textContent = error.message; card.append(problem);
    }
  }
}
function renderMembers(card, project, members, canManage) {
  const heading = document.createElement('h4'); heading.textContent = '项目成员'; card.append(heading);
  const list = document.createElement('div'); list.className = 'member-list';
  if (!members.length) list.textContent = '暂无成员';
  for (const member of members) {
    const row = document.createElement('p'); row.textContent = `${member.email} · ${member.role_name} `;
    if (canManage) {
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
  const roleSelect = document.createElement('select'); roleSelect.name = 'role_id'; roleSelect.setAttribute('aria-label', '选择角色'); roleOptions.forEach(r => roleSelect.append(option(r.id, r.name)));
  const add = document.createElement('button'); add.textContent = '加入并指派角色'; form.append(userSelect, roleSelect, add);
  form.addEventListener('submit', event => { event.preventDefault(); submit(form, `/api/projects/${project.id}/members`, refresh); }); card.append(form);
}
function renderWorkItems(card, project, members, requirements, tasks, canManage) {
  const heading = document.createElement('h4'); heading.textContent = '需求与任务看板'; card.append(heading);
  const requirementList = document.createElement('ul'); requirementList.className = 'requirements';
  if (!requirements.length) { const empty = document.createElement('li'); empty.textContent = '暂无需求条目'; requirementList.append(empty); }
  requirements.forEach(item => { const li = document.createElement('li'); const strong = document.createElement('strong'); strong.textContent = item.title; li.append(strong); if (item.description) li.append(document.createTextNode(` — ${item.description}`)); requirementList.append(li); });
  card.append(requirementList);
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
    forms.append(requirementForm, taskForm); card.append(forms);
  }
  const board = document.createElement('div'); board.className = 'board';
  for (const [status, label] of Object.entries(statusLabels)) {
    const column = document.createElement('section'); column.className = 'board-column'; column.dataset.status = status;
    const columnTitle = document.createElement('h5'); columnTitle.textContent = `${label} (${tasks.filter(task => task.status === status).length})`; column.append(columnTitle);
    if (canManage) {
      column.addEventListener('dragover', event => event.preventDefault());
      column.addEventListener('drop', async event => { event.preventDefault(); const taskId = event.dataTransfer.getData('text/plain'); if (!taskId) return; try { await api(`/api/projects/${project.id}/tasks/${taskId}`, { method: 'PUT', body: JSON.stringify({ status }) }); say('任务状态已更新'); await refresh(); } catch (error) { say(error.message, true); } });
    }
    for (const task of tasks.filter(item => item.status === status)) {
      const item = document.createElement('article'); item.className = 'task-card'; item.draggable = canManage; item.addEventListener('dragstart', event => event.dataTransfer.setData('text/plain', task.id));
      const name = document.createElement('strong'); name.textContent = task.title; item.append(name);
      const meta = document.createElement('span'); meta.textContent = `${task.assignee_email || '未指派'}${task.due_date ? ` · ${task.due_date}` : ''}`; item.append(meta); column.append(item);
    }
    board.append(column);
  }
  card.append(board);
}
function renderCheckProjects() { const select = $('checkProject'); select.replaceChildren(); projects.forEach(p => select.append(option(p.id, p.name))); }
$('setupForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/setup', refresh); });
$('loginForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/login', refresh); });
$('roleForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/roles', refresh); });
$('userForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/users', refresh); });
$('projectForm').addEventListener('submit', e => { e.preventDefault(); submit(e.currentTarget, '/api/projects', refresh); });
$('logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST', body: '{}' }); await refresh(); });
async function check(path) { try { const data = await api(path); $('checkResult').textContent = JSON.stringify(data, null, 2); } catch (e) { $('checkResult').textContent = e.message; } }
$('checkView').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/members`));
$('checkManage').addEventListener('click', () => check(`/api/projects/${$('checkProject').value}/manage-check`));
refresh().catch(e => say(e.message, true));
