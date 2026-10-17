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
    const projects = await (await api.get(`${url}/api/projects`)).json();
    const base = `${url}/api/projects/${projects[0].id}/tasks`;
    const tasks = await (await api.get(base)).json();
    const response = await api.put(`${base}/${tasks[0].id}`, { data: { title: 'Concurrent change', version: tasks[0].version } });
    assert.equal(response.status(), 200);
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.locator('#taskDialogError').filter({ hasText: '任务已被其他操作修改' }).waitFor();
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('Browser smoke passed: creation, edit, state, filters, stale conflict, mobile layout');
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
