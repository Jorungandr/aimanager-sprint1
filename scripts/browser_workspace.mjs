import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(path.join(os.tmpdir(), 'aimanager-workspace-'));
const output = path.join(root, 'test-results', process.env.CAPTURE_LABEL || 'workspace');
await mkdir(output, { recursive: true });
const port = await new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', () => {
        const address = socket.address();
        socket.close(error => {
            if (error) {
                reject(error);
            } else {
                resolve(address.port);
            }
        });
    });
});
const baseURL = `http://127.0.0.1:${port}`;
const program = 'import sys; from server import create_server; server=create_server(sys.argv[1],int(sys.argv[2])); server.serve_forever()';
const server = spawn(process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
    ['-u', '-c', program, path.join(temporary, 'test.db'), String(port)], { cwd: root, stdio: 'ignore' });
let browser;
try {
    const deadline = Date.now() + 15000;
    while (true) {
        try {
            const response = await fetch(`${baseURL}/api/status`);
            if (response.ok) {
                break;
            }
        } catch (error) {
            if (Date.now() >= deadline || server.exitCode !== null) {
                throw new Error('工作台测试服务启动失败。', { cause: error });
            }
        }
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 960 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('/');
    await page.locator('#setupPanel').waitFor({ state: 'visible' });
    if (!process.env.CAPTURE_ONLY) {
        await page.locator('.auth-art img').evaluate(image => image.decode());
        const asset = await context.request.get('/assets/hummingbird.png');
        assert.equal(asset.status(), 200);
        assert.equal(asset.headers()['content-type'], 'image/png');
        assert.equal((await context.request.get('/assets/../service.py')).status(), 404);
    }
    await page.screenshot({ path: path.join(output, 'auth-desktop.png'), fullPage: true });
    await page.locator('#setupForm input[name="email"]').fill('liuhaotian@example.test');
    await page.locator('#setupForm input[name="password"]').fill('WorkspacePass123');
    await page.locator('#setupForm button').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    async function create(resource, data) {
        const token = await context.request.get('/api/csrf');
        const response = await context.request.post(resource, {
            data,
            headers: { 'X-CSRF-Token': (await token.json()).csrf_token },
        });
        assert.equal(response.status(), 201, await response.text());
        return response.json();
    }
    await create('/api/users', { email: 'member@example.test', password: 'MemberPass123' });
    const project = await create('/api/projects', { name: '爱管理 · 团队协作' });
    const second = await create('/api/projects', { name: '蜂鸟 · 发布验收' });
    const requirement = await create(`/api/projects/${project.id}/requirements`, {
        title: '协作工作台体验升级', description: '让项目进度与团队分工更清晰。',
    });
    const names = ['梳理本轮用户反馈', '确认角色与访问边界', '优化任务创建流程', '实现项目数据概览', '核对移动端操作体验', '补充回归测试', '验证需求归档与恢复', '准备本轮评审演示'];
    for (const [index, title] of names.entries()) {
        await create(`/api/projects/${project.id}/tasks`, {
            title, requirement_id: requirement.id, status: ['backlog', 'todo', 'doing', 'done'][index % 4],
        });
    }
    await page.reload();
    await page.locator('.task-card').first().waitFor();
    if (!process.env.CAPTURE_ONLY) {
        await page.locator('.project-hero-art').evaluate(image => image.decode());
        assert.equal(await page.locator('.pulse-completion strong').innerText(), '25%');
        await page.getByRole('button', { name: '新建任务', exact: true }).click();
        assert.equal(await page.locator('[data-create-task] input[name="title"]').evaluate(
            element => element === document.activeElement), true);
        const createPanel = page.locator('.project details').filter({ has: page.locator('[data-create-task]') });
        await createPanel.locator('summary').click();
        await page.locator('.focus-task').first().click();
        await page.locator('#taskDialog').waitFor({ state: 'visible' });
        await page.locator('#closeTaskDialog').click();
    }
    await page.screenshot({ path: path.join(output, 'board-desktop.png'), fullPage: true });
    await page.locator('.project-hero').screenshot({ path: path.join(output, 'project-cover.png') });
    await page.locator('[data-view="insights"]').click();
    await page.locator('.kpi-value').first().waitFor();
    await page.screenshot({ path: path.join(output, 'insights-desktop.png'), fullPage: true });
    if (!process.env.CAPTURE_ONLY) {
        assert.equal(await page.locator('#projectPicker').isVisible(), true,
            '数据概览应能直接切换当前项目。');
        await page.locator('#projectPicker').selectOption(String(second.id));
        await page.waitForFunction(() => document.querySelector('.kpi-value')?.textContent === '0');
        assert.match(await page.locator('#insightScope').innerText(), /蜂鸟/);
        await page.locator('#projectPicker').selectOption(String(project.id));
        await page.waitForFunction(() => document.querySelector('.kpi-value')?.textContent === '8');

        let release;
        let started;
        let finished;
        const pending = new Promise(resolve => { release = resolve; });
        const intercepted = new Promise(resolve => { started = resolve; });
        const settled = new Promise(resolve => { finished = resolve; });
        await page.route('**/stats/tasks?**', async route => {
            const response = await route.fetch();
            const value = new URL(route.request().url()).searchParams.get('value');
            if (value === 'backlog') {
                started();
                await pending;
                await route.fulfill({ response });
                finished();
            } else {
                await route.fulfill({ response });
            }
        });
        const statusCard = page.locator('.insight-card').filter({ has: page.getByRole('heading', { name: '状态分布' }) });
        await statusCard.locator('.chart-bar').first().click();
        await intercepted;
        await statusCard.locator('.chart-bar').last().click();
        await page.locator('#insightDrill h4').filter({ hasText: '已完成 · 2 项' }).waitFor();
        release();
        await settled;
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.match(await page.locator('#insightDrill h4').innerText(), /已完成 · 2 项/,
            '较早点击的延迟响应不得覆盖较新的明细。');
        assert.equal(await page.locator('#insightDrill').getAttribute('aria-busy'), null);
        await page.unroute('**/stats/tasks?**');

        let keyboardRequests = 0;
        const countKeyboardRequests = request => {
            if (request.url().includes('/stats/tasks?')) {
                keyboardRequests += 1;
            }
        };
        page.on('request', countKeyboardRequests);
        const completedKpi = page.getByRole('button', { name: '已完成 2，查看任务明细' });
        await completedKpi.focus();
        const keyboardResponse = page.waitForResponse(response => response.url().includes('/stats/tasks?'));
        await completedKpi.press('Enter');
        await keyboardResponse;
        await page.waitForFunction(() => document.querySelector('#insightDrill')?.getAttribute('aria-busy') === null);
        await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)));
        page.off('request', countKeyboardRequests);
        assert.equal(keyboardRequests, 1, '原生按钮按一次回车只能发出一次明细请求。');
        await page.route(`**/api/projects/${project.id}/stats`, route => route.fulfill({
            status: 503, contentType: 'application/json', body: JSON.stringify({ error: '统计暂时不可用' }),
        }));
        await page.locator('[data-view="insights"]').click();
        await page.getByRole('button', { name: '重试加载统计' }).waitFor();
        await page.unroute(`**/api/projects/${project.id}/stats`);
        await page.getByRole('button', { name: '重试加载统计' }).click();
        await page.locator('.kpi-value').first().waitFor();


        await page.locator('[data-view="collaboration"]').click();
        let releaseProject;
        let projectStarted;
        const projectPending = new Promise(resolve => { releaseProject = resolve; });
        const projectIntercepted = new Promise(resolve => { projectStarted = resolve; });
        await page.route(`**/api/projects/${second.id}/tasks`, async route => {
            projectStarted();
            await projectPending;
            await route.continue();
        });
        await page.locator('#projectPicker').selectOption(String(second.id));
        await projectIntercepted;
        assert.equal(await page.locator('#projects').getAttribute('aria-busy'), 'true');
        assert.equal(await page.getByRole('status').filter({ hasText: '正在加载蜂鸟' }).count(), 1);
        await page.locator('#projectPicker').selectOption(String(project.id));
        await page.locator('.task-card').first().waitFor();
        releaseProject();
        await page.unroute(`**/api/projects/${second.id}/tasks`, { behavior: 'wait' });
        assert.match(await page.locator('.project-hero h3').innerText(), /爱管理/);
        assert.equal(await page.locator('#checkProject').inputValue(), String(project.id));
        assert.equal(await page.locator('#projects').getAttribute('aria-busy'), null);

        await page.route(`**/api/projects/${second.id}/tasks`, route => route.fulfill({
            status: 503, contentType: 'application/json', body: JSON.stringify({ error: '测试中的暂时故障' }),
        }));
        await page.locator('#projectPicker').selectOption(String(second.id));
        await page.getByRole('button', { name: '重试加载项目' }).waitFor();
        assert.equal(await page.locator('#projects').getAttribute('aria-busy'), null);
        await page.unroute(`**/api/projects/${second.id}/tasks`);
        await page.getByRole('button', { name: '重试加载项目' }).click();
        await page.locator('.project-hero h3').filter({ hasText: '蜂鸟' }).waitFor();
        await page.locator('#projectPicker').selectOption(String(project.id));
        await page.locator('.task-card').first().waitFor();
        await page.locator('[data-view="insights"]').click();
        await page.locator('.kpi-value').first().waitFor();

    }
    for (const width of [768, 390]) {
        await page.setViewportSize({ width, height: 900 });
        await page.screenshot({ path: path.join(output, `insights-${width}.png`), fullPage: true });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
        assert.equal(overflow, false, `${width}px 不应出现页面横向溢出。`);
    }
    if (!process.env.CAPTURE_ONLY) {
        await page.locator('#navToggle').click();
        assert.equal(await page.locator('[data-view="collaboration"]').isVisible(), true);
        await page.locator('[data-view="collaboration"]').click();
        assert.equal(await page.locator('#navToggle').getAttribute('aria-expanded'), 'false');
        await page.locator('.task-card').first().waitFor();
        await page.screenshot({ path: path.join(output, 'board-mobile.png'), fullPage: true });
        await page.locator('.project-hero').screenshot({ path: path.join(output, 'project-cover-mobile.png') });
        let releaseAccount;
        let accountStarted;
        let accountFinished;
        const accountPending = new Promise(resolve => { releaseAccount = resolve; });
        const accountIntercepted = new Promise(resolve => { accountStarted = resolve; });
        const accountSettled = new Promise(resolve => { accountFinished = resolve; });
        let heldAccount = false;
        await page.route('**/api/me', async route => {
            if (heldAccount) {
                await route.continue();
                return;
            }
            heldAccount = true;
            const response = await route.fetch();
            accountStarted();
            await accountPending;
            await route.fulfill({ response });
            accountFinished();
        });
        await page.locator('#refreshWorkspace').click();
        await accountIntercepted;
        await page.locator('#logout').click();
        await page.locator('#loginPanel').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#insightBody > *').count(), 0,
            '退出后清空上一账号的数据概览。');
        await page.screenshot({ path: path.join(output, 'login-mobile.png'), fullPage: true });
        assert.equal(await page.locator('#projects > *').count(), 0);
        assert.equal(await page.locator('#currentUser').innerText(), '—');
        await page.locator('#loginForm input[name="email"]').fill('member@example.test');
        await page.locator('#loginForm input[name="password"]').fill('MemberPass123');
        await page.locator('#loginForm button').click();
        await page.locator('#workspace').waitFor({ state: 'visible' });
        await page.waitForFunction(() => document.querySelector('#currentUser')?.textContent === 'member@example.test');
        releaseAccount();
        await accountSettled;
        await page.waitForFunction(() => !document.querySelector('#refreshWorkspace').disabled);
        await page.unroute('**/api/me');
        assert.equal(await page.locator('#currentUser').innerText(), 'member@example.test',
            '旧账号的延迟刷新不能覆盖重新登录的账号。');
        assert.equal(await page.locator('#roleForm').isVisible(), false);
        assert.equal(await page.locator('.task-card').count(), 0);

    }
    assert.deepEqual(errors, [], '浏览器不应出现未捕获错误。');
    console.log('PASS 工作台切换、项目范围、响应式布局与截图');
} finally {
    if (browser) {
        await browser.close();
    }
    if (server.exitCode === null) {
        const exited = new Promise(resolve => server.once('exit', resolve));
        server.kill();
        await exited;
    }
    await rm(temporary, { recursive: true, force: true });
}
