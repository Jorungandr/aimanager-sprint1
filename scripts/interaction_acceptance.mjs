/* 前端交互收尾修复专项验收（可独立运行，也纳入 npm run test:browser）。
   覆盖：① 防重复提交（归档/恢复/移出成员，人工延迟约 1 秒下连点只发一次写请求；
        失败可重试、取消确认不发送请求且不残留禁用态、成功提示不被重复请求覆盖）；
   ② 折叠面板展开状态按项目隔离保存在页面内存（保存/归档/刷新后保持，切换项目不串状态，
        退出登录后不恢复旧界面状态）；
   ③ 新增需求说明为 textarea（rows=3、maxlength=500、字段名 description 不变、可换行）。
   请求计数通过 page.route 拦截统计，运行结束打印 JSON 证据。
   用法：node scripts/interaction_acceptance.mjs */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const root = fileURLToPath(new URL('../', import.meta.url));
const temporary = await mkdtemp(join(tmpdir(), 'aimanager-interaction-'));
const server = spawn(process.env.PYTHON || 'python', ['-u', '-B', '-c',
    "import sys;from server import create_server;s=create_server(sys.argv[1],0);print('READY http://127.0.0.1:'+str(s.server_port),flush=True);s.serve_forever()",
    join(temporary, 'interaction.db')], { cwd: root, stdio: ['ignore', 'pipe', 'inherit'] });
let browser;
const counts = { archive: 0, restore: 0, remove: 0 };
const DELAY_MS = 1000;
try {
    const url = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Test server did not start')), 15000);
        server.once('error', reject);
        server.once('exit', code => reject(new Error(`Test server exited: ${code}`)));
        const lines = createInterface({ input: server.stdout });
        lines.on('line', line => {
            if (line.startsWith('READY ')) {
                clearTimeout(timer);
                resolve(line.slice(6));
            }
        });
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    /* 写请求统一人工延迟约 1 秒，并分别统计三种操作的请求数 */
    let failNextArchive = false;
    const delayThen = async (route, kind, fail = false) => {
        counts[kind] += 1;
        await new Promise(resolve => setTimeout(resolve, DELAY_MS));
        if (fail) {
            return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: '模拟服务器故障' }) });
        }
        return route.continue();
    };
    await page.route('**/api/projects/*/requirements/*/archive', route => {
        if (route.request().method() !== 'POST') return route.continue();
        const fail = failNextArchive;
        failNextArchive = false;
        return delayThen(route, 'archive', fail);
    });
    await page.route('**/api/projects/*/requirements/*/restore', route => {
        if (route.request().method() !== 'POST') return route.continue();
        return delayThen(route, 'restore');
    });
    await page.route('**/api/projects/*/members/*', route => {
        if (route.request().method() !== 'DELETE') return route.continue();
        return delayThen(route, 'remove');
    });

    const writeApi = async (method, path, options) => {
        const bootstrap = await page.context().request.get(`${url}/api/csrf`);
        assert.equal(bootstrap.status(), 200);
        const token = (await bootstrap.json()).csrf_token;
        return page.context().request[method](path, { ...options, headers: { 'X-CSRF-Token': token } });
    };
    const acceptNext = () => page.once('dialog', dialog => dialog.accept());
    const dismissNext = () => page.once('dialog', dialog => dialog.dismiss());
    const waitDisabled = (rowsSelector, text, buttonName) => page.waitForFunction(({ rowsSelector, text, buttonName }) => {
        for (const row of document.querySelectorAll(rowsSelector)) {
            if (row.textContent.includes(text)) {
                for (const button of row.querySelectorAll('button')) {
                    if (button.textContent.trim() === buttonName) {
                        return button.disabled;
                    }
                }
            }
        }
        return false;
    }, { rowsSelector, text, buttonName });
    const panel = text => page.locator('#projects details').filter({ has: page.locator('summary', { hasText: text }) });
    const panelOpen = async text => panel(text).evaluate(element => element.open);
    const expand = async text => {
        const details = panel(text);
        if (!(await details.evaluate(element => element.open))) {
            await details.locator('summary').click();
        }
        assert.equal(await panelOpen(text), true, `面板应展开：${text}`);
    };
    const assertPanelClosed = async text => assert.equal(await panelOpen(text), false, `面板应折叠：${text}`);
    const requirementRow = title => page.locator('.requirements li').filter({ hasText: title });
    const archiveButton = title => requirementRow(title).getByRole('button', { name: '归档', exact: true });
    const restoreButton = title => requirementRow(title).getByRole('button', { name: '恢复', exact: true });

    /* ---------- 准备：管理员、项目 Alpha、成员、需求 ---------- */
    await page.goto(url);
    await page.locator('#setupForm [name=email]').fill('admin@example.test');
    await page.locator('#setupForm [name=password]').fill('DemoPass123');
    await page.getByRole('button', { name: '创建并登录', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: '管理配置', exact: true }).click();
    let projectWrites = 0;
    await page.route('**/api/projects', async route => {
        if (route.request().method() === 'POST') {
            projectWrites += 1;
            await new Promise(resolve => setTimeout(resolve, DELAY_MS));
        }
        await route.continue();
    });
    await page.locator('#projectForm [name=name]').fill('Alpha');
    await page.getByRole('button', { name: '创建项目', exact: true }).click();
    await page.locator('#projectForm').evaluate(form => form.requestSubmit());
    await page.getByRole('heading', { name: 'Alpha', exact: true }).waitFor();
    assert.equal(projectWrites, 1, 'Repeated form submission must create only one project');
    await page.unroute('**/api/projects');

    const userResponse = await writeApi('post', `${url}/api/users`, { data: { email: 'member2@example.test', password: 'DemoPass123' } });
    assert.equal(userResponse.status(), 201, '创建第二位成员账号');
    const member2 = (await userResponse.json()).id;
    const roleResponse = await writeApi('post', `${url}/api/roles`, { data: { name: '项目成员' } });
    assert.equal(roleResponse.status(), 201, '创建项目角色');
    const roleId = (await roleResponse.json()).id;
    const grant = await writeApi('put', `${url}/api/roles/${roleId}/permissions`, { data: { permissions: ['project.view', 'members.manage', 'workitems.manage'] } });
    assert.equal(grant.status(), 200, '授予角色成员权限');
    const projects = await (await writeApi('get', `${url}/api/projects`)).json();
    const alpha = projects.find(project => project.name === 'Alpha');
    assert.ok(alpha, '项目 Alpha 应存在');
    const roleOptions = await (await writeApi('get', `${url}/api/projects/${alpha.id}/role-options`)).json();
    assert.ok(roleOptions.length, '项目应有可授权角色');
    const addMember = await writeApi('post', `${url}/api/projects/${alpha.id}/members`, { data: { user_id: member2, role_id: roleId } });
    assert.equal(addMember.status(), 201, '成员加入 Alpha');
    await page.getByRole('button', { name: '刷新数据', exact: true }).click();
    await page.getByText(/项目成员 · 2 人/).waitFor();

    /* ---------- ③ 新增需求说明：textarea rows=3 maxlength=500，可换行，500 上限仍有效 ---------- */
    await expand('新建需求或任务');
    await page.locator('[data-create-kind=requirement]').click();
    const requirementForm = page.locator('.workitem-forms form').first();
    const descriptionField = requirementForm.locator('textarea[name=description]');
    assert.equal(await descriptionField.getAttribute('rows'), '3', 'textarea rows 应为 3');
    assert.equal(await descriptionField.getAttribute('maxlength'), '500', 'textarea maxlength 应为 500');
    await descriptionField.pressSequentially('a'.repeat(501), { delay: 1 });
    assert.equal((await descriptionField.inputValue()).length, 500, '真实输入超过 500 字应被浏览器截断');
    await descriptionField.fill('第一行说明\n第二行说明');
    await requirementForm.locator('[name=title]').fill('A1 多行说明需求');
    await requirementForm.getByRole('button', { name: '保存需求', exact: true }).click();
    await page.getByText('需求列表 · 1 条', { exact: true }).waitFor();
    const alphaRequirements = await (await writeApi('get', `${url}/api/projects/${alpha.id}/requirements`)).json();
    const a1 = alphaRequirements.find(item => item.title === 'A1 多行说明需求');
    assert.equal(a1.description, '第一行说明\n第二行说明', '多行说明应原样保存');
    await requirementForm.locator('[name=title]').fill('A2 待归档需求');
    await requirementForm.getByRole('button', { name: '保存需求', exact: true }).click();
    await page.getByText('需求列表 · 2 条', { exact: true }).waitFor();
    const overLimit = await writeApi('post', `${url}/api/projects/${alpha.id}/requirements`, { data: { title: '超长说明', description: 'x'.repeat(501) } });
    assert.equal(overLimit.status(), 400, '服务端应拒绝超过 500 字的说明');

    /* ---------- ② 折叠状态：保存任务（刷新）后保持展开 ---------- */
    await expand('项目成员');
    await expand('需求列表');
    await expand('新建需求或任务');
    await page.locator('[data-create-kind=task]').click();
    const taskForm = page.locator('.workitem-forms form').last();
    await taskForm.locator('[name=title]').fill('折叠保持用任务');
    await taskForm.getByRole('button', { name: '保存任务', exact: true }).click();
    await page.getByText('需求列表 · 2 条', { exact: true }).waitFor();
    assert.equal(await panelOpen('项目成员'), true, '保存后成员面板应保持展开');
    assert.equal(await panelOpen('需求列表'), true, '保存后需求面板应保持展开');
    assert.equal(await panelOpen('新建需求或任务'), true, '保存后新建面板应保持展开');

    /* ---------- ① 取消确认：不发请求、不残留禁用态 ---------- */
    dismissNext();
    await archiveButton('A1 多行说明需求').click();
    await page.waitForTimeout(150);
    assert.equal(counts.archive, 0, '取消确认不得发送归档请求');
    assert.equal(await archiveButton('A1 多行说明需求').isEnabled(), true, '取消确认后归档按钮不得残留禁用');
    await expand('项目成员');
    const memberRow = page.locator('.member-list p').filter({ hasText: 'member2@example.test' });
    dismissNext();
    await memberRow.getByRole('button', { name: '移出项目', exact: true }).click();
    await page.waitForTimeout(150);
    assert.equal(counts.remove, 0, '取消确认不得发送移出请求');
    assert.equal(await memberRow.getByRole('button', { name: '移出项目', exact: true }).isEnabled(), true, '取消确认后移出按钮不得残留禁用');

    /* ---------- ① 请求失败后可重试：归档失败一次，按钮恢复可用 ---------- */
    failNextArchive = true;
    acceptNext();
    await archiveButton('A1 多行说明需求').click();
    await page.getByText('模拟服务器故障', { exact: false }).waitFor();
    assert.equal(counts.archive, 1, '失败请求应计入 1 次');
    assert.equal(await archiveButton('A1 多行说明需求').isEnabled(), true, '失败后归档按钮应恢复可用');

    /* ---------- ① 防重复提交：延迟约 1 秒下连点归档只发一次请求，成功提示不被覆盖 ---------- */
    acceptNext();
    await archiveButton('A1 多行说明需求').click();
    await waitDisabled('.requirements li', 'A1 多行说明需求', '归档');
    await archiveButton('A1 多行说明需求').click({ force: true });
    await restoreButton('A1 多行说明需求').waitFor();
    await page.waitForTimeout(200);
    assert.equal(counts.archive, 2, '失败重试成功后，连点归档仅新增 1 次请求（总数 2）');
    assert.match(await page.locator('#notice').textContent(), /需求已归档/, '成功提示应为归档成功文案，不被重复请求错误覆盖');

    /* ---------- ① 防重复提交：连点恢复只发一次请求 ---------- */
    await restoreButton('A1 多行说明需求').click();
    await waitDisabled('.requirements li', 'A1 多行说明需求', '恢复');
    await restoreButton('A1 多行说明需求').click({ force: true });
    await archiveButton('A1 多行说明需求').waitFor();
    await page.waitForTimeout(200);
    assert.equal(counts.restore, 1, '连点恢复只应发出 1 次请求');
    assert.match(await page.locator('#notice').textContent(), /需求已恢复/, '恢复成功提示正常');

    /* ---------- ① 防重复提交：连点移出成员只发一次请求 ---------- */
    acceptNext();
    await memberRow.getByRole('button', { name: '移出项目', exact: true }).click();
    await waitDisabled('.member-list p', 'member2@example.test', '移出项目');
    await memberRow.getByRole('button', { name: '移出项目', exact: true }).click({ force: true });
    await page.getByText(/项目成员 · 1 人/).waitFor();
    await page.waitForTimeout(200);
    assert.equal(counts.remove, 1, '连点移出只应发出 1 次请求');
    assert.match(await page.locator('#notice').textContent(), /成员已移出/, '移出成功提示正常');

    /* ---------- ② 归档（刷新）后折叠状态保持 ---------- */
    acceptNext();
    await archiveButton('A2 待归档需求').click();
    await restoreButton('A2 待归档需求').waitFor();
    await page.waitForTimeout(200);
    assert.equal(counts.archive, 3, '归档 A2 为第 3 次归档请求');
    assert.equal(await panelOpen('项目成员'), true, '归档后成员面板应保持展开');
    assert.equal(await panelOpen('需求列表'), true, '归档后需求面板应保持展开');
    assert.equal(await panelOpen('新建需求或任务'), true, '归档后新建面板应保持展开');

    /* ---------- ② 状态按项目隔离 ---------- */
    const betaResponse = await writeApi('post', `${url}/api/projects`, { data: { name: 'Beta' } });
    assert.equal(betaResponse.status(), 201, '创建项目 Beta');
    const beta = (await betaResponse.json());
    await writeApi('post', `${url}/api/projects/${beta.id}/requirements`, { data: { title: 'Beta 需求', description: '' } });
    await page.getByRole('button', { name: '刷新数据', exact: true }).click();
    await page.locator('#projectPicker').selectOption(String(beta.id));
    await page.getByRole('heading', { name: 'Beta', exact: true }).waitFor();
    await assertPanelClosed('项目成员');
    await assertPanelClosed('需求列表');
    await expand('新建需求或任务');
    await page.locator('#projectPicker').selectOption(String(alpha.id));
    await page.getByRole('heading', { name: 'Alpha', exact: true }).waitFor();
    assert.equal(await panelOpen('项目成员'), true, '切回 Alpha 后成员面板保持展开，不被 Beta 状态覆盖');
    assert.equal(await panelOpen('需求列表'), true, '切回 Alpha 后需求面板保持展开');
    assert.equal(await panelOpen('新建需求或任务'), true, '切回 Alpha 后新建面板保持展开');
    await page.locator('#projectPicker').selectOption(String(beta.id));
    await page.getByRole('heading', { name: 'Beta', exact: true }).waitFor();
    await assertPanelClosed('项目成员');
    await assertPanelClosed('需求列表');
    assert.equal(await panelOpen('新建需求或任务'), true, 'Beta 仅新建面板展开，不与 Alpha 串状态');

    /* ---------- ② 退出登录后不恢复旧界面状态 ---------- */
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.locator('#loginPanel').waitFor({ state: 'visible' });
    await page.locator('#loginForm [name=email]').fill('admin@example.test');
    await page.locator('#loginForm [name=password]').fill('DemoPass123');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.getByRole('heading', { name: 'Alpha', exact: true }).waitFor();
    await assertPanelClosed('项目成员');
    await assertPanelClosed('需求列表');
    await assertPanelClosed('新建需求或任务');

    assert.deepEqual(errors, []);
    console.log(`Interaction acceptance passed. Request counts: ${JSON.stringify(counts)}`);
} finally {
    if (browser) {
        await browser.close();
    }
    if (server.exitCode === null) {
        server.kill();
        await once(server, 'exit');
    }
    await rm(temporary, { recursive: true, force: true });
}
