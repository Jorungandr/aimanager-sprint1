import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failures = [];
let browser;
let server;
let temporaryDirectory;

function allocatePort() {
    return new Promise((resolve, reject) => {
        const socket = net.createServer();
        socket.once('error', reject);
        socket.listen(0, '127.0.0.1', () => {
            const { port } = socket.address();
            socket.close(error => error ? reject(error) : resolve(port));
        });
    });
}

async function waitForServer(url, child) {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
        if (child.exitCode !== null) throw new Error(`测试服务器提前退出：${child.exitCode}`);
        try {
            const response = await fetch(`${url}/api/status`);
            if (response.ok) return;
        } catch (_) {
            // 服务器仍在启动。
        }
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('测试服务器在 15 秒内未就绪。');
}

async function startServer() {
    const port = await allocatePort();
    temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), 'aimanager-browser-'));
    const database = path.join(temporaryDirectory, 'acceptance.db');
    const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    const program = [
        'import sys',
        'from server import create_server',
        'server = create_server(sys.argv[1], int(sys.argv[2]))',
        'server.serve_forever()',
    ].join('; ');
    const child = spawn(python, ['-u', '-c', program, database, String(port)], {
        cwd: root,
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    server = child;
    let startupError;
    child.once('error', error => { startupError = error; });
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.once('exit', code => {
        if (code && !failures.some(item => item.name === '测试服务器')) {
            failures.push({ name: '测试服务器', error: new Error(`退出码 ${code}\n${stderr}`) });
        }
    });
    const baseURL = `http://127.0.0.1:${port}`;
    await waitForServer(baseURL, child);
    if (startupError) {
        throw startupError;
    }
    return { child, baseURL };
}

async function api(context, baseURL, method, resource, data) {
    const response = await context.request.fetch(`${baseURL}${resource}`, { method, data });
    let body;
    try { body = await response.json(); } catch (_) { body = null; }
    return { status: response.status(), body };
}

async function expectApi(context, baseURL, method, resource, data, expectedStatus) {
    const result = await api(context, baseURL, method, resource, data);
    assert.equal(
        result.status,
        expectedStatus,
        `${method} ${resource} 应返回 ${expectedStatus}，实际为 ${result.status}：${JSON.stringify(result.body)}`,
    );
    return result.body;
}

async function login(page, email, password) {
    await page.goto('/');
    await page.locator('#loginPanel').waitFor({ state: 'visible' });
    await page.locator('#loginForm input[name="email"]').fill(email);
    await page.locator('#loginForm input[name="password"]').fill(password);
    await page.locator('#loginForm button').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
}

async function check(name, callback) {
    try {
        await callback();
        console.log(`PASS ${name}`);
    } catch (error) {
        failures.push({ name, error });
        console.error(`FAIL ${name}\n${error.stack || error}`);
    }
}

async function bootstrap(adminContext, adminPage, baseURL) {
    await adminPage.goto('/');
    await adminPage.locator('#setupPanel').waitFor({ state: 'visible' });
    await adminPage.locator('#setupForm input[name="email"]').fill('admin@example.test');
    await adminPage.locator('#setupForm input[name="password"]').fill('AdminPass123');
    await adminPage.locator('#setupForm button').click();
    await adminPage.locator('#workspace').waitFor({ state: 'visible' });

    const viewer = await expectApi(adminContext, baseURL, 'POST', '/api/users', {
        email: 'viewer@example.test', password: 'ViewerPass123',
    }, 201);
    const viewerRole = await expectApi(adminContext, baseURL, 'POST', '/api/roles', {
        name: '只读成员',
    }, 201);
    await expectApi(adminContext, baseURL, 'PUT', `/api/roles/${viewerRole.id}/permissions`, {
        permissions: ['project.view'],
    }, 200);
    const project = await expectApi(adminContext, baseURL, 'POST', '/api/projects', {
        name: '蜂鸟浏览器验收项目',
    }, 201);
    await expectApi(adminContext, baseURL, 'POST', `/api/projects/${project.id}/members`, {
        user_id: viewer.id, role_id: viewerRole.id,
    }, 201);
    const requirement = await expectApi(adminContext, baseURL, 'POST', `/api/projects/${project.id}/requirements`, {
        title: '浏览器验收', description: '覆盖权限、筛选、键盘与移动端',
    }, 201);
    const task = await expectApi(adminContext, baseURL, 'POST', `/api/projects/${project.id}/tasks`, {
        title: '准备验收数据', requirement_id: requirement.id, assignee_user_id: viewer.id,
        due_date: '2026-10-20', status: 'todo',
    }, 201);
    await expectApi(adminContext, baseURL, 'POST', `/api/projects/${project.id}/tasks`, {
        title: '第二条可筛选任务', requirement_id: requirement.id, status: 'backlog',
    }, 201);
    await adminPage.reload();
    await adminPage.locator('#workspace').waitFor({ state: 'visible' });
    await adminPage.getByRole('heading', { name: project.name }).waitFor();
    await adminPage.getByText(task.title, { exact: true }).waitFor();
    return { project, task, viewer };
}

async function main() {
    const running = await startServer();
    server = running.child;
    browser = await chromium.launch({ headless: true });
    const adminContext = await browser.newContext({ baseURL: running.baseURL });
    const adminPage = await adminContext.newPage();
    const fixtures = await bootstrap(adminContext, adminPage, running.baseURL);

    const viewerContext = await browser.newContext({ baseURL: running.baseURL });
    const viewerPage = await viewerContext.newPage();
    await login(viewerPage, 'viewer@example.test', 'ViewerPass123');

    await check('只读成员可查看，但不能创建、编辑或变更状态', async () => {
        await viewerPage.getByRole('heading', { name: fixtures.project.name }).waitFor();
        await viewerPage.getByText(fixtures.task.title, { exact: true }).waitFor();
        assert.equal(await viewerPage.locator('.workitem-forms').count(), 0, '只读成员不应看到新增需求或任务表单。');
        assert.equal(await viewerPage.locator('.project button.danger').count(), 0, '只读成员不应看到成员移除按钮。');
        assert.equal(await viewerPage.locator('.task-card[draggable="true"]').count(), 0, '只读成员的任务卡不得可拖动。');

        const statuses = await viewerPage.evaluate(async ({ projectId, taskId }) => {
            const send = (url, method, body) => fetch(url, {
                method,
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            }).then(response => response.status);
            return Promise.all([
                send(`/api/projects/${projectId}/requirements`, 'POST', { title: '越权需求' }),
                send(`/api/projects/${projectId}/tasks`, 'POST', { title: '越权任务' }),
                send(`/api/projects/${projectId}/tasks/${taskId}`, 'PUT', { title: '越权编辑' }),
                send(`/api/projects/${projectId}/tasks/${taskId}`, 'PUT', { status: 'doing' }),
            ]);
        }, { projectId: fixtures.project.id, taskId: fixtures.task.id });
        assert.deepEqual(statuses, [403, 403, 403, 403], '所有只读写请求都必须由服务器返回 403。');
        const tasks = await expectApi(viewerContext, running.baseURL, 'GET', `/api/projects/${fixtures.project.id}/tasks`, undefined, 200);
        assert.equal(tasks.find(item => item.id === fixtures.task.id).status, 'todo', '越权请求不得改变任务状态。');
    });

    await check('成员被移出后刷新页面无法继续访问项目', async () => {
        await expectApi(adminContext, running.baseURL, 'DELETE',
            `/api/projects/${fixtures.project.id}/members/${fixtures.viewer.id}`, undefined, 200);
        await viewerPage.reload();
        await viewerPage.locator('#workspace').waitFor({ state: 'visible' });
        await viewerPage.locator('#projects .empty-state').filter({ hasText: '还没有可访问的项目' }).waitFor();
        assert.equal(await viewerPage.getByText(fixtures.project.name, { exact: true }).count(), 0, '已移出成员刷新后仍看到了项目。');
        const response = await viewerPage.evaluate(projectId => fetch(
            `/api/projects/${projectId}/tasks`, { credentials: 'same-origin' },
        ).then(result => result.status), fixtures.project.id);
        assert.equal(response, 403, '刷新后的直接项目请求必须被服务器拒绝。');
    });

    await adminPage.reload();
    await adminPage.locator('#workspace').waitFor({ state: 'visible' });
    await adminPage.getByRole('heading', { name: fixtures.project.name }).waitFor();
    await adminPage.getByText(fixtures.task.title, { exact: true }).waitFor();

    await check('筛选无结果后可通过“清除筛选”恢复任务', async () => {
        const filter = adminPage.locator([
            '[data-testid="task-filter"]',
            'input[aria-label*="筛选"]',
            'input[placeholder*="筛选"]',
            'input[placeholder*="搜索"]',
        ].join(', '));
        assert.equal(await filter.count(), 1, '任务看板应提供一个具有可访问名称的筛选输入框。');
        await filter.fill('绝对不存在的验收任务');
        assert.equal(await adminPage.locator('.task-card:visible').count(), 0, '无匹配条件下仍显示了任务卡。');
        const clear = adminPage.locator('[data-testid="clear-filters"], button').filter({ hasText: '清除筛选' });
        assert.equal(await clear.count(), 1, '无结果状态应显示唯一的“清除筛选”按钮。');
        await clear.focus();
        await adminPage.keyboard.press('Enter');
        await adminPage.getByText(fixtures.task.title, { exact: true }).waitFor();
        assert.equal(await filter.inputValue(), '', '清除筛选后输入值应为空。');
    });

    await check('任务状态支持键盘操作', async () => {
        const search = adminPage.getByRole('searchbox', { name: '搜索任务' });
        if (await search.count()) await search.fill('');
        const card = adminPage.locator('.task-card').filter({ hasText: fixtures.task.title });
        assert.equal(await card.count(), 1, '未找到待键盘操作的任务卡。');
        const before = await card.evaluate(element => element.closest('.board-column')?.dataset.status);
        assert.equal(before, 'todo', '键盘验收任务初始状态应为待办。');
        const statusSelect = card.getByRole('combobox', { name: `${fixtures.task.title}的状态` });
        assert.equal(await statusSelect.count(), 1, '可管理任务应提供具有可访问名称的状态控件。');
        await statusSelect.focus();
        assert.equal(await statusSelect.evaluate(element => document.activeElement === element), true,
            '任务状态控件必须可以通过键盘获得焦点。');
        await adminPage.keyboard.press('ArrowDown');
        await adminPage.locator('.board-column[data-status="doing"] .task-card', {
            hasText: fixtures.task.title,
        }).waitFor();
        const tasks = await expectApi(adminContext, running.baseURL, 'GET', `/api/projects/${fixtures.project.id}/tasks`, undefined, 200);
        assert.equal(tasks.find(item => item.id === fixtures.task.id).status, 'doing', '键盘操作必须持久化任务状态。');
    });

    await check('移动端无横向溢出且主要控件满足 44×44 CSS 像素', async () => {
        await adminPage.setViewportSize({ width: 390, height: 844 });
        await adminPage.reload();
        await adminPage.locator('#workspace').waitFor({ state: 'visible' });
        await adminPage.getByRole('heading', { name: fixtures.project.name }).waitFor();
        await adminPage.getByText(fixtures.task.title, { exact: true }).waitFor();
        const audit = await adminPage.evaluate(() => {
            const visible = element => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };
            const controls = [...document.querySelectorAll(
                'button, input:not([type="hidden"]):not([type="checkbox"]), select, summary, .task-card[tabindex="0"]',
            )].filter(visible).map(element => {
                const rect = element.getBoundingClientRect();
                return {
                    label: element.getAttribute('aria-label') || element.textContent.trim() || element.name || element.tagName,
                    width: Math.round(rect.width * 10) / 10,
                    height: Math.round(rect.height * 10) / 10,
                };
            });
            return {
                overflow: document.documentElement.scrollWidth - window.innerWidth,
                undersized: controls.filter(item => item.width < 44 || item.height < 44),
            };
        });
        assert.ok(audit.overflow <= 1, `移动端页面横向溢出 ${audit.overflow}px。`);
        assert.deepEqual(audit.undersized, [], `以下主要控件小于 44×44：${JSON.stringify(audit.undersized)}`);
    });

    await viewerContext.close();
    await adminContext.close();
}

try {
    await main();
} catch (error) {
    failures.push({ name: '验收初始化', error });
    console.error(error.stack || error);
} finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null && server.pid) {
        const exited = new Promise(resolve => server.once('exit', resolve));
        server.kill();
        await exited;
    }
    if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true });
}

if (failures.length) {
    console.error(`\n浏览器验收失败 ${failures.length} 项：`);
    failures.forEach(({ name, error }) => console.error(`- ${name}: ${error.message}`));
    process.exitCode = 1;
} else {
    console.log('\n浏览器验收全部通过。');
}
