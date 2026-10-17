/* 页面展示与请求。所有真正的权限判断由服务器完成。 */
const $ = id => document.getElementById(id);
let current = null, roles = [], roleOptions = [], users = [], projects = [];
const permissionLabels = { 'project.view': '查看项目', 'members.manage': '管理成员', 'projects.manage': '管理项目', 'roles.manage': '管理角色', 'workitems.manage': '管理需求任务' };
const statusLabels = { backlog: '待梳理', todo: '待办', doing: '进行中', done: '已完成' };
let activeProject = '', activeView = 'collaboration', projectRequest = 0, editingTask = null, editingRequirement = null, dialogOpener = null;
const filters = new Map();
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
function enterLoggedOut() {
  current = null;
  closeDialogs();
  filters.clear();
  activeProject = '';
  $('workspace').hidden = true;
  $('logout').hidden = true;
  $('setupPanel').hidden = true;
  $('loginPanel').hidden = false;
  $('projects').replaceChildren();
  $('projectPicker').replaceChildren(option('', '暂无项目'));
  say('登录已过期，请重新登录。未保存的编辑内容已关闭。', true);
}
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
    if (response.status === 401 && current) enterLoggedOut();
    const error = new Error(describeError(response.status, data.error));
    error.status = response.status;
    error.reason = data.error || '';
    throw error;
  }
  return data;
}
function say(message, error = false) {
  const notice = $('notice');
  notice.textContent = message;
  notice.className = error ? 'error' : 'ok';
}
function formData(form) { return Object.fromEntries(new FormData(form).entries()); }
async function submit(form, path, after, method = 'POST') {
  const buttons = [...form.querySelectorAll('button')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const created = await api(path, { method, body: JSON.stringify(formData(form)) });
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
      ? '切换项目后，成员、需求与看板会同步刷新；页面只显示当前项目的数据，跨项目内容不会混入。'
      : '当前账号还没有可访问的项目，因此没有可切换的选项。请由项目管理者创建项目并把你加入成员。';
  }
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
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.textContent = '保存权限';
    button.addEventListener('click', async () => {
      try {
        await api(`/api/roles/${role.id}/permissions`, { method: 'PUT', body: JSON.stringify({ permissions: Object.keys(checks).filter(k => checks[k].checked) }) });
        say('权限已保存');
        await refresh();
      } catch (e) {
        say(e.message, true);
      }
    });
    action.append(button);
    tr.append(action);
    body.append(tr);
  }
}
async function renderProjects() {
  const request = ++projectRequest;
  const target = $('projects'); target.replaceChildren();
  if (!projects.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = '当前账号还没有可访问的项目。管理员可在“管理配置”中创建项目并把成员加入项目；普通成员请联系项目管理者分配权限。';
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
  const openRequirements = requirements.filter(item => !item.archived);
  const archivedCount = requirements.length - openRequirements.length;
  const requirementDetails = document.createElement('details');
  const requirementSummary = document.createElement('summary');
  requirementSummary.textContent = `需求列表 · ${requirements.length} 条`;
  // 归档数量用 title 补充说明，不改变 summary 文本本身
  if (archivedCount) requirementSummary.title = `其中 ${archivedCount} 条已归档：仅保留历史任务的关联，不可用于新任务`;
  requirementDetails.append(requirementSummary);
  if (!requirements.length) {
    requirementDetails.open = true;
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
            restore.disabled = true;
            try {
              await api(`/api/projects/${project.id}/requirements/${item.id}/restore`, { method: 'POST', body: JSON.stringify({ version: item.version }) });
              say('需求已恢复，可以重新关联任务');
              await refresh();
            } catch (error) {
              say(error.message, true);
              restore.disabled = false;
            }
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
            try {
              await api(`/api/projects/${project.id}/requirements/${item.id}/archive`, { method: 'POST', body: JSON.stringify({ version: item.version }) });
              say('需求已归档，历史任务仍保留关联');
              await refresh();
            } catch (error) {
              say(error.message, true);
            }
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
    const requirementForm = document.createElement('form'); requirementForm.innerHTML = '<h5>新增需求</h5><label>标题<input name="title" maxlength="80" required></label><label>说明<input name="description" maxlength="500"></label><button>保存需求</button>';
    requirementForm.addEventListener('submit', event => { event.preventDefault(); submit(requirementForm, `/api/projects/${project.id}/requirements`, refresh); });
    const taskForm = document.createElement('form'); const taskTitle = document.createElement('h5'); taskTitle.textContent = '新增任务'; taskForm.append(taskTitle);
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
  if (!tasks.length) {
    // 没有任何任务时，筛选与四列看板都没有信息量，用一块空状态说明下一步
    const empty = document.createElement('div');
    empty.className = 'empty-state compact board-empty';
    empty.textContent = canManage
      ? '当前项目还没有任务。展开“新建需求或任务”保存第一条任务，之后可按状态在看板中推进。'
      : '当前项目还没有任务。任务由项目管理者创建，创建后会在这里按状态分列显示。';
    card.append(empty);
    return;
  }
  toolbar.append(search, assigneeFilter);
  card.append(toolbar);
  const boardHint = document.createElement('p');
  boardHint.className = 'hint';
  boardHint.textContent = canManage
    ? '拖拽卡片可快速改变状态；卡片内的状态下拉框提供等价的键盘操作。'
    : '只读访问：状态与指派由具备“管理需求任务”权限的成员维护。';
  card.append(boardHint);
  // 筛选无结果：说明原因并提供一键清除，避免用户逐列查找空列
  const filterEmpty = document.createElement('div');
  filterEmpty.className = 'filter-empty';
  filterEmpty.hidden = true;
  const filterEmptyText = document.createElement('p');
  const clearFilters = document.createElement('button');
  clearFilters.type = 'button';
  clearFilters.className = 'secondary';
  clearFilters.textContent = '清除筛选';
  filterEmpty.append(filterEmptyText, clearFilters);
  card.append(filterEmpty);
  const board = document.createElement('div'); board.className = 'board';
  const draw = () => {
  board.replaceChildren();
  const visible = tasks.filter(task => {
    const requirement = requirements.find(item => item.id === task.requirement_id);
    return `${task.title} ${requirement?.title || ''}`.toLowerCase().includes(state.search.toLowerCase()) &&
      (!state.assignee || (state.assignee === 'unassigned' ? !task.assignee_user_id : String(task.assignee_user_id) === state.assignee));
  });
  const filtering = Boolean(state.search || state.assignee);
  filterEmptyText.textContent = `没有符合当前筛选条件的任务（本项目共 ${tasks.length} 项）。`;
  filterEmpty.hidden = !(filtering && !visible.length);
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
        // 历史任务的关联即使需求已归档也要显示，并明确标注
        tag.className = requirement.archived ? 'requirement-tag is-archived' : 'requirement-tag';
        tag.textContent = requirement.archived ? `${requirement.title}（已归档）` : requirement.title;
        if (requirement.archived) tag.title = '关联需求已归档：任务关联保留，恢复需求后可继续维护';
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
      empty.textContent = filtering ? '无匹配任务' : '暂无任务';
      column.append(empty);
    }
    board.append(column);
  }
  };
  search.addEventListener('input', () => { state.search = search.value.trim(); draw(); });
  assigneeFilter.addEventListener('change', () => { state.assignee = assigneeFilter.value; draw(); });
  clearFilters.addEventListener('click', () => {
    state.search = '';
    state.assignee = '';
    search.value = '';
    assigneeFilter.value = '';
    draw();
    search.focus();
  });
  draw();
  card.append(board);
}
/* ---------- 弹窗：焦点管理、冲突锁与统一提示 ---------- */
function openDialog(id, focusName) {
  dialogOpener = document.activeElement;
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
  const field = focusName ? dialog.querySelector(`[name="${focusName}"]`) : null;
  // 焦点落在标题输入框而不是“关闭”，键盘用户可直接编辑
  if (field) requestAnimationFrame(() => { field.focus(); if (field.select) field.select(); });
}
function closeDialog(id) {
  const dialog = $(id);
  if (dialog.open) dialog.close();
}
/* Esc 与按钮关闭都经由此处归还焦点，键盘用户不会丢位置 */
for (const id of ['taskDialog', 'requirementDialog']) {
  $(id).addEventListener('close', () => {
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
  if (submitButton) submitButton.disabled = false;
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
      say('已获取最新数据，请重新选择要编辑的条目');
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
$('closeRequirementDialog').addEventListener('click', () => closeDialog('requirementDialog'));
$('editRequirementForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingRequirement) return;
  const form = event.currentTarget;
  if (form.dataset.conflict === 'true') return;
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const data = formData(form);
    data.version = editingRequirement.requirement.version;
    await api(`/api/projects/${editingRequirement.project.id}/requirements/${editingRequirement.requirement.id}`, { method: 'PUT', body: JSON.stringify(data) });
    closeDialog('requirementDialog');
    say('需求修改已保存');
    await renderProjects();
  } catch (error) {
    showDialogProblem(form, 'requirementDialogError', error);
  } finally {
    button.disabled = form.dataset.conflict === 'true';
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
$('closeTaskDialog').addEventListener('click', () => closeDialog('taskDialog'));
$('editTaskForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingTask) return;
  const form = event.currentTarget;
  if (form.dataset.conflict === 'true') return;
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await api(`/api/projects/${editingTask.project.id}/tasks/${editingTask.task.id}`, { method: 'PUT', body: JSON.stringify({ ...formData(form), version: editingTask.task.version }) });
    closeDialog('taskDialog');
    say('任务修改已保存');
    await renderProjects();
  } catch (error) {
    showDialogProblem(form, 'taskDialogError', error);
  } finally {
    button.disabled = form.dataset.conflict === 'true';
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
function renderCheckProjects() {
  const select = $('checkProject');
  if (projects.length) {
    select.replaceChildren(...projects.map(p => option(p.id, p.name)));
  } else {
    select.replaceChildren(option('', '暂无项目'));
  }
  select.disabled = !projects.length;
  $('checkView').disabled = !projects.length;
  $('checkManage').disabled = !projects.length;
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
