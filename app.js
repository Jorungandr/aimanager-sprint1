/* 页面展示与请求。所有真正的权限判断由服务器完成。 */
const $ = id => document.getElementById(id);
let current = null, roles = [], roleOptions = [], users = [], projects = [];
const permissionLabels = { 'project.view': '查看项目', 'members.manage': '管理成员', 'projects.manage': '管理项目', 'roles.manage': '管理角色' };
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
  renderMatrix(); renderProjects(); renderCheckProjects();
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
function renderProjects() {
  const target = $('projects'); target.replaceChildren();
  if (!projects.length) { const p = document.createElement('p'); p.textContent = '当前没有可访问项目。'; target.append(p); return; }
  for (const project of projects) {
    const card = document.createElement('section'); card.className = 'project'; const title = document.createElement('h3'); title.textContent = project.name; card.append(title);
    const list = document.createElement('div'); list.textContent = '正在读取成员…'; card.append(list);
    api(`/api/projects/${project.id}/members`).then(members => {
      list.replaceChildren(); if (!members.length) list.textContent = '暂无成员';
      for (const member of members) {
        const row = document.createElement('p'); row.textContent = `${member.email} · ${member.role_name} `;
        list.append(row);
      }
      api(`/api/projects/${project.id}/manage-check`).then(() => {
        for (const member of members) {
          const row = [...list.children].find(item => item.textContent.startsWith(`${member.email} · `));
          const remove = document.createElement('button'); remove.className = 'danger'; remove.textContent = '移出项目';
          remove.addEventListener('click', async () => { if (!confirm(`移出 ${member.email}？`)) return; try { await api(`/api/projects/${project.id}/members/${member.user_id}`, { method: 'DELETE' }); say('成员已移出，权限已回收'); await refresh(); } catch (e) { say(e.message, true); } });
          row.append(remove);
        }
        const form = document.createElement('form'); form.className = 'inline';
        const userSelect = document.createElement('select'); userSelect.name = 'user_id'; userSelect.setAttribute('aria-label', '选择账号'); users.forEach(u => userSelect.append(option(u.id, u.email)));
        const roleSelect = document.createElement('select'); roleSelect.name = 'role_id'; roleSelect.setAttribute('aria-label', '选择角色'); roleOptions.forEach(r => roleSelect.append(option(r.id, r.name)));
        const add = document.createElement('button'); add.textContent = '加入并指派角色'; form.append(userSelect, roleSelect, add);
        form.addEventListener('submit', event => { event.preventDefault(); submit(form, `/api/projects/${project.id}/members`, refresh); }); card.append(form);
      }).catch(() => {});
    }).catch(e => { list.textContent = e.message; });
    target.append(card);
  }
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
