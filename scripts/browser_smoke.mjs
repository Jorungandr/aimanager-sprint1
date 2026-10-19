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
const temporary = await mkdtemp(join(tmpdir(), 'aimanager-ui-'));
const server = spawn(process.env.PYTHON || 'python', ['-u', '-B', '-c',
    "import sys;from server import create_server;s=create_server(sys.argv[1],0);print('READY http://127.0.0.1:'+str(s.server_port),flush=True);s.serve_forever()",
    join(temporary, 'ui.db')], { cwd: root, stdio: ['ignore', 'pipe', 'inherit'] });
let browser;
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
    await page.goto(url);
    await page.locator('#setupForm [name=email]').fill('ui@example.test');
    await page.locator('#setupForm [name=password]').fill('DemoPass123');
    await page.getByRole('button', { name: '创建并登录', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: '管理配置', exact: true }).click();
    await page.locator('#projectForm [name=name]').fill('Browser smoke project');
    await page.locator('#projectForm button').click();
    await page.getByRole('heading', { name: 'Browser smoke project', exact: true }).waitFor();
    await page.getByText('新建需求或任务', { exact: true }).click();
    const creation = page.locator('.workitem-forms form').last();
    await creation.locator('[name=title]').fill('Browser smoke task');
    await creation.getByRole('button', { name: '保存任务', exact: true }).click();
    await page.locator('.task-card').filter({ hasText: 'Browser smoke task' }).waitFor();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.locator('#editTaskForm [name=title]').fill('Edited task');
    await page.locator('#editTaskForm [name=status]').selectOption('doing');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.locator('#taskDialog').waitFor({ state: 'hidden' });
    const edited = page.locator('.task-card').filter({ hasText: 'Edited task' });
    await edited.waitFor();
    await edited.getByRole('combobox').selectOption('done');
    await page.locator('.board-column[data-status=done] .task-card').waitFor();
    await page.getByRole('searchbox', { name: '搜索任务' }).fill('not-matching');
    assert.equal(await page.locator('.task-card').count(), 0);
    await page.getByRole('searchbox', { name: '搜索任务' }).fill('Edited task');
    assert.equal(await page.locator('.task-card').count(), 1);
    await page.getByRole('combobox', { name: '按负责人筛选' }).selectOption('unassigned');
    assert.equal(await page.locator('.task-card').count(), 1);
    // A stale editor must not overwrite a concurrent change.
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    const api = page.context().request;
    const writeApi = async (method, path, options) => {
        const bootstrap = await api.get(`${url}/api/csrf`);
        assert.equal(bootstrap.status(), 200);
        const token = (await bootstrap.json()).csrf_token;
        return api[method](path, { ...options, headers: { 'X-CSRF-Token': token } });
    };
    const projects = await (await api.get(`${url}/api/projects`)).json();
    const base = `${url}/api/projects/${projects[0].id}/tasks`;
    const tasks = await (await api.get(base)).json();
    const response = await writeApi('put', `${base}/${tasks[0].id}`, { data: { title: 'Concurrent change', version: tasks[0].version } });
    assert.equal(response.status(), 200);
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.locator('#taskDialogError').filter({ hasText: '任务已被其他操作修改' }).waitFor();
    assert.equal(await page.locator('#editTaskForm [type=submit]').isDisabled(), true);
    assert.equal(await page.locator('#editTaskForm [name=title]').inputValue(), 'Edited task');
    await page.locator('#editTaskForm [data-dialog-refresh]').click();
    await page.locator('#taskDialog').waitFor({ state: 'hidden' });
    await page.getByRole('searchbox', { name: '搜索任务' }).fill('');
    await page.getByRole('button', { name: '刷新数据', exact: true }).click();
    await page.getByText('新建需求或任务', { exact: true }).click();
    const requirementForm = page.locator('.workitem-forms form').first();
    await requirementForm.locator('[name=title]').fill('Lifecycle requirement');
    await requirementForm.getByRole('button', { name: '保存需求', exact: true }).click();
    await page.getByText('需求列表 · 1 条', { exact: true }).click();
    const requirementRow = page.locator('.requirements li');
    await requirementRow.getByRole('button', { name: '编辑', exact: true }).click();
    await page.locator('#editRequirementForm [name=title]').fill('Updated lifecycle');
    await page.locator('#editRequirementForm [type=submit]').click();
    await page.locator('#requirementDialog').waitFor({ state: 'hidden' });
    await page.getByText('需求列表 · 1 条', { exact: true }).click();
    await requirementRow.getByText('Updated lifecycle', { exact: true }).waitFor();
    page.once('dialog', dialog => dialog.accept());
    await requirementRow.getByRole('button', { name: '归档', exact: true }).click();
    await page.getByText('需求列表 · 1 条', { exact: true }).click();
    await requirementRow.getByRole('button', { name: '恢复', exact: true }).waitFor();
    await page.getByText('新建需求或任务', { exact: true }).click();
    assert.equal(await page.locator('.workitem-forms select[name=requirement_id] option').count(), 1);
    await requirementRow.getByRole('button', { name: '恢复', exact: true }).click();
    await page.getByText('新建需求或任务', { exact: true }).click();
    await page.locator('.workitem-forms select[name=requirement_id] option').filter({ hasText: 'Updated lifecycle' }).waitFor({ state: 'attached' });
    // Delayed responses from a closed editor must not alter a newer editor.
    const projectBase = `${url}/api/projects/${projects[0].id}`;
    assert.equal((await writeApi('post', `${projectBase}/tasks`, { data: { title: 'Another task' } })).status(), 201);
    assert.equal((await writeApi('post', `${projectBase}/requirements`, { data: { title: 'Another requirement' } })).status(), 201);
    for (const kind of ['task', 'requirement']) {
        for (const status of [200, 409]) {
            await page.reload();
            await page.locator('.task-card').first().waitFor();
            await page.evaluate(() => {
                document.querySelectorAll('#projects details').forEach(details => {
                    details.open = true;
                });
            });
            const rows = page.locator(kind === 'task' ? '.task-card' : '.requirements li');
            const form = page.locator(kind === 'task' ? '#editTaskForm' : '#editRequirementForm');
            const dialog = page.locator(kind === 'task' ? '#taskDialog' : '#requirementDialog');
            const errorBox = page.locator(kind === 'task' ? '#taskDialogError' : '#requirementDialogError');
            const pattern = `**/api/projects/*/${kind}s/*`;
            let receive;
            const intercepted = new Promise(resolve => {
                receive = resolve;
            });
            let requests = 0;
            await page.route(pattern, route => {
                if (route.request().method() !== 'PUT') {
                    return route.continue();
                }
                requests += 1;
                receive(route);
            });
            await rows.first().getByRole('button', { name: '编辑', exact: true }).click();
            await form.locator('[type=submit]').click();
            const pending = await intercepted;
            await form.evaluate(element => {
                element.requestSubmit();
            });
            await page.keyboard.press('Escape');
            await dialog.waitFor({ state: 'hidden' });
            await rows.last().getByRole('button', { name: '编辑', exact: true }).click();
            await form.locator('[name=title]').fill('New editor unsaved input');
            const responseReady = page.waitForResponse(response => response.request().method() === 'PUT');
            await pending.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? {} : { error: 'Delayed conflict' }) });
            await (await responseReady).finished();
            await page.evaluate(() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            }));
            assert.equal(requests, 1, 'Repeated submit must not issue another request');
            assert.equal(await dialog.isVisible(), true, 'Old response must not close the new editor');
            assert.equal(await form.locator('[name=title]').inputValue(), 'New editor unsaved input');
            assert.equal(await form.locator('[type=submit]').isEnabled(), true);
            assert.equal(await errorBox.textContent(), '');
            await page.unroute(pattern);
            await page.keyboard.press('Escape');
        }
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.locator('.task-card').first().getByRole('button', { name: '编辑', exact: true }).click();
    assert.equal(await page.locator('#editTaskForm [type=submit]').isEnabled(), true);
    const loggedOut = await writeApi('post', `${url}/api/logout`, { data: {} });
    assert.equal(loggedOut.status(), 200);
    await page.locator('#editTaskForm [type=submit]').click();
    await page.locator('#loginPanel').waitFor({ state: 'visible' });
    await page.locator('#taskDialog').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#workspace').isHidden(), true);
    assert.match(await page.locator('#notice').textContent(), /登录.*过期/);
    assert.deepEqual(errors, []);
    console.log('Browser smoke passed: creation, edit, archive, restore, state, filters, stale conflict, mobile layout');
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
