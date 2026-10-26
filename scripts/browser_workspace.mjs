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
    await page.locator('[data-home-start-project]').waitFor();
    await page.screenshot({ path: path.join(output, 'welcome-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, '新用户手机首页不得横向溢出。');
    const welcomeAction = await page.locator('[data-home-start-project]').boundingBox();
    const welcomeSteps = await page.locator('.welcome-steps').boundingBox();
    assert.ok(welcomeAction && welcomeSteps && welcomeAction.y < welcomeSteps.y, '手机首页应先提供创建入口，再展示使用说明。');
    await page.screenshot({ path: path.join(output, 'welcome-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.locator('[data-home-start-project]').click();
    assert.equal(await page.locator('#projectForm [name="name"]').evaluate(field => field === document.activeElement), true);
    await page.locator('#projectForm [name="name"]').fill('爱管理 · 团队协作');
    await page.locator('#projectForm [type="submit"]').click();
    await page.locator('.project-hero h3').filter({ hasText: '爱管理' }).waitFor();
    assert.equal(await page.locator('.home-entry').count(), 3, '空项目应提供清晰的三个起步入口。');
    const project = (await (await context.request.get('/api/projects')).json()).find(item => item.name === '爱管理 · 团队协作');
    const second = await create('/api/projects', { name: '蜂鸟 · 发布验收' });
    const requirement = await create(`/api/projects/${project.id}/requirements`, {
        title: '协作工作台体验升级', description: '让项目进度与团队分工更清晰。',
    });
    const names = ['梳理本轮用户反馈', '确认角色与访问边界', '优化任务创建流程', '实现项目数据概览', '核对移动端操作体验', '补充回归测试', '验证需求归档与恢复', '准备本轮评审演示'];
    const account = await (await context.request.get('/api/me')).json();
    const day = offset => {
        const date = new Date();
        date.setDate(date.getDate() + offset);
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    };
    for (const [index, title] of names.entries()) {
        await create(`/api/projects/${project.id}/tasks`, {
            title, requirement_id: requirement.id, status: ['backlog', 'todo', 'doing', 'done'][index % 4],
            assignee_user_id: index === 0 ? account.id : null,
            due_date: index === 0 ? day(-1) : (index === 1 ? day(3) : null),
        });
    }
    await page.reload();
    await page.locator('.task-card').first().waitFor();
    if (!process.env.CAPTURE_ONLY) {
        await page.locator('.project-hero-art').evaluate(image => image.decode());
        assert.equal(await page.locator('.pulse-completion strong').innerText(), '25%');
        for (const [scope, count] of [['open', 6], ['mine', 1], ['overdue', 1], ['due_soon', 1]]) {
            await page.getByPlaceholder('搜索任务标题或关联需求').fill('不存在的搜索条件');
            await page.locator(`[data-home-scope="${scope}"]`).click();
            assert.equal(await page.locator('.task-card').count(), count, '首页任务入口必须显示对应任务，不能被旧筛选遮挡。');
        }
        await page.locator('[data-scope="all"]').click();
        await page.locator('[data-home-create="requirement"]').click();
        assert.equal(await page.locator('[data-draft-key$=":requirement"] [name="title"]').evaluate(field => field === document.activeElement), true);
        await page.locator('[data-home-create="task"]').click();
        assert.equal(await page.locator('[data-create-task] [name="title"]').evaluate(field => field === document.activeElement), true);
        await page.locator('details').filter({ has: page.locator('[data-create-task]') }).locator('summary').click();

        await page.getByRole('button', { name: '新建任务', exact: true }).click();
        assert.equal(await page.locator('[data-create-task] input[name="title"]').evaluate(
            element => element === document.activeElement), true);
        const createPanel = page.locator('.project details').filter({ has: page.locator('[data-create-task]') });
        await createPanel.locator('summary').click();
        await page.locator('.focus-task').first().click();
        await page.locator('#taskDialog').waitFor({ state: 'visible' });
        await page.locator('#closeTaskDialog').click();
    }
    if (!process.env.CAPTURE_ONLY) {
        for (const [scope, count] of [['mine', 1], ['overdue', 1], ['due_soon', 1], ['unassigned', 7], ['all', 8]]) {
            await page.locator(`[data-scope="${scope}"]`).click();
            assert.equal(await page.locator('.task-card').count(), count, `${scope} 快捷筛选数量应准确。`);
            assert.equal(await page.locator('.pulse-completion strong').innerText(), '25%', '筛选不能改变项目总体完成率。');
        }
        await page.getByRole('button', { name: '列表视图', exact: true }).click();
        assert.equal(await page.locator('.board.task-list .task-card').count(), 8);
        assert.equal(await page.locator('.task-card[draggable="true"]').count(), 0, '列表只通过明确的状态控件修改，不能误拖。');
        assert.equal(await page.locator('.task-card[data-readonly="true"]').count(), 0, '管理者列表不能误标只读。');
        assert.equal(await page.locator('.task-card').first().evaluate(element => getComputedStyle(element, '::after').content), 'none');
        await page.locator('.task-card').first().getByRole('button', { name: '编辑', exact: true }).click();
        const editTitle = page.locator('#editTaskForm [name="title"]');
        const beforeEdit = await editTitle.inputValue();
        await editTitle.fill('尚未提交的修改');
        page.once('dialog', dialog => dialog.dismiss());
        await page.locator('#closeTaskDialog').click();
        assert.equal(await page.locator('#taskDialog').isVisible(), true, '取消放弃应继续编辑。');
        assert.equal(await editTitle.inputValue(), '尚未提交的修改');
        page.once('dialog', dialog => dialog.dismiss());
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#taskDialog').isVisible(), true, 'Esc也应保护未保存内容。');
        page.once('dialog', dialog => dialog.accept());
        await page.keyboard.press('Escape');
        await page.locator('#taskDialog').waitFor({ state: 'hidden' });
        await page.locator('.task-card').first().getByRole('button', { name: '编辑', exact: true }).click();
        assert.equal(await editTitle.inputValue(), beforeEdit, '放弃修改不能写入服务器。');
        await page.locator('#closeTaskDialog').click();
        await page.getByRole('combobox', { name: '任务排序', exact: true }).selectOption('newest');
        assert.equal(await page.locator('.task-card strong').first().innerText(), names[7]);
        await page.getByRole('combobox', { name: '任务排序', exact: true }).selectOption('due');
        assert.equal(await page.locator('.task-card strong').first().innerText(), names[0]);
        await page.locator('.task-card').first().getByRole('button', { name: '编辑', exact: true }).click();
        await page.locator('#taskDialog').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#editTaskForm input[name="title"]').inputValue(), names[0]);
        await page.locator('#closeTaskDialog').click();
        await page.getByRole('searchbox', { name: '搜索任务', exact: true }).fill('不存在的任务');
        await page.getByRole('button', { name: '清除筛选', exact: true }).waitFor();
        await page.getByRole('button', { name: '清除筛选', exact: true }).click();
        assert.equal(await page.locator('.board.task-list .task-card').count(), 8);
        await page.locator('#projectPicker').selectOption(String(second.id));
        await page.locator('.board-empty').waitFor();
        await page.locator('#projectPicker').selectOption(String(project.id));
        await page.locator('.board.task-list .task-card').first().waitFor();
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({ path: path.join(output, 'list-desktop.png'), fullPage: true });
        for (const width of [768, 390, 320]) {
            await page.setViewportSize({ width, height: 900 });
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false,
                `${width}px 列表视图不应出现页面横向溢出。`);
        }
        const comfort = await page.locator('.task-card').first().evaluate(card => {
            const actions = card.querySelector('.task-actions');
            const select = actions.querySelector('select');
            const button = actions.querySelector('button');
            const first = select.getBoundingClientRect();
            const second = button.getBoundingClientRect();
            return {
                sameRow: Math.abs(first.top - second.top) < 2,
                controlSize: parseFloat(getComputedStyle(select).fontSize),
                titleSize: parseFloat(getComputedStyle(card.querySelector('strong')).fontSize),
            };
        });
        assert.equal(comfort.sameRow, true, '手机卡片操作应并排，减少滚动距离。');
        assert.ok(comfort.controlSize >= 16, '手机表单字号至少16px，避免聚焦时放大页面。');
        assert.ok(comfort.titleSize >= 15, '手机任务标题应清晰可读。');
        await page.locator('.task-card').first().screenshot({ path: path.join(output, 'task-mobile-detail.png') });
        await page.locator('.task-card').first().getByRole('button', { name: '编辑', exact: true }).click();
        await page.locator('#taskDialog').waitFor({ state: 'visible' });
        await page.locator('#taskDialog').screenshot({ path: path.join(output, 'editor-mobile-detail.png') });
        await page.locator('#closeTaskDialog').click();
        await page.screenshot({ path: path.join(output, 'list-mobile.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 960 });
        await page.getByRole('button', { name: '看板视图', exact: true }).click();
    }
    await page.evaluate(() => scrollTo(0, 0));
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
    if (!process.env.CAPTURE_ONLY) {
        await page.locator('[data-view="collaboration"]').click();
        const createPanel = page.locator('details').filter({ has: page.locator('summary', { hasText: '新建需求或任务' }) });
        const taskForm = page.locator('form[data-create-task]');
        const requirementForm = page.locator('form').filter({ has: page.locator('h5', { hasText: '新增需求' }) });
        await createPanel.locator('summary').click();
        await taskForm.locator('[name="title"]').fill('用户流程：待保存任务');
        await page.locator('[data-create-kind=requirement]').click();
        assert.equal(await taskForm.isVisible(), false, '新建区每次只显示一种表单。');
        await createPanel.screenshot({ path: path.join(output, 'creation-panel.png') });
        await requirementForm.locator('[name="title"]').fill('用户流程：待保存需求');
        await requirementForm.locator('[name="description"]').fill('第一行背景\n第二行验收');
        await page.locator('#projectPicker').selectOption(String(second.id));
        await page.locator('.board-empty').waitFor();
        await page.locator('#projectPicker').selectOption(String(project.id));
        await page.locator('.task-card').first().waitFor();
        assert.equal(await taskForm.locator('[name="title"]').inputValue(), '用户流程：待保存任务', '切换项目不能丢失未保存的任务草稿。');
        assert.equal(await requirementForm.locator('[name="description"]').inputValue(), '第一行背景\n第二行验收');
        await page.locator('#refreshWorkspace').click();
        await page.waitForFunction(() => !document.querySelector('#refreshWorkspace').disabled);
        assert.equal(await taskForm.locator('[name="title"]').inputValue(), '用户流程：待保存任务', '刷新数据应保留草稿。');
        let releaseSave;
        let saveStarted;
        let saveRequests = 0;
        const savePending = new Promise(resolve => { releaseSave = resolve; });
        const saveIntercepted = new Promise(resolve => { saveStarted = resolve; });
        await page.route(`**/api/projects/${project.id}/requirements`, async route => {
            if (route.request().method() === 'POST') {
                saveRequests += 1;
                saveStarted();
                await savePending;
                return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: '暂时无法保存' }) });
            }
            return route.continue();
        });
        await requirementForm.getByRole('button', { name: '保存需求' }).click();
        await saveIntercepted;
        assert.equal(await requirementForm.getByRole('button', { name: '保存中…' }).isDisabled(), true);
        await requirementForm.evaluate(form => form.requestSubmit());
        assert.equal(saveRequests, 1, '保存中重复提交不得发送第二次请求。');
        releaseSave();
        await requirementForm.locator('[data-form-error]').filter({ hasText: '暂时无法保存' }).waitFor();
        assert.equal(await requirementForm.locator('[name="title"]').inputValue(), '用户流程：待保存需求');
        assert.equal(await requirementForm.getByRole('button', { name: '保存需求' }).isEnabled(), true);
        assert.equal(await page.locator('#notice').evaluate(notice => {
            const rect = notice.getBoundingClientRect();
            return getComputedStyle(notice).position === 'fixed' && rect.bottom <= innerHeight && rect.top >= 0;
        }), true, '操作反馈应在当前视口内可见。');
        await page.getByRole('button', { name: '关闭提示', exact: true }).click();
        assert.equal(await page.locator('#notice').textContent(), '');
        await page.unroute(`**/api/projects/${project.id}/requirements`);
        await requirementForm.getByRole('button', { name: '保存需求' }).click();
        await page.waitForFunction(() => document.querySelector('form[data-create-task] [name="requirement_id"]')?.options.length === 3);
        assert.equal(await requirementForm.locator('[name="title"]').inputValue(), '', '成功提交的草稿应清除。');
        assert.equal(await taskForm.locator('[name="title"]').inputValue(), '用户流程：待保存任务', '保存需求不能清空另一张表单的任务草稿。');
        await page.locator('[data-create-kind=task]').click();
        assert.equal(await requirementForm.isVisible(), false);
        await taskForm.locator('[name="requirement_id"]').selectOption({ label: '用户流程：待保存需求' });
        await page.locator('[data-scope="mine"]').click();
        await page.getByRole('searchbox', { name: '搜索任务', exact: true }).fill('不会匹配新任务');
        await taskForm.getByRole('button', { name: '保存任务' }).click();
        await page.locator('.task-card strong').filter({ hasText: '用户流程：待保存任务' }).waitFor();
        assert.equal(await page.locator('.task-card').count(), 9, '保存新任务后清除遮挡结果的筛选。');
        assert.equal(await taskForm.locator('[name="title"]').inputValue(), '');
        await taskForm.locator('[name="title"]').fill('退出后不应保留的草稿');
        await page.locator('#logout').click();
        await page.locator('#loginPanel').waitFor({ state: 'visible' });
        await page.locator('#loginForm input[name="email"]').fill('liuhaotian@example.test');
        await page.locator('#loginForm input[name="password"]').fill('WorkspacePass123');
        await page.locator('#loginForm button').click();
        await page.locator('.task-card').first().waitFor();
        await createPanel.locator('summary').click();
        assert.equal(await taskForm.locator('[name="title"]').inputValue(), '', '退出后重新登录不得恢复旧会话草稿。');
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
        assert.equal(await page.locator('.task-actions').first().evaluate(actions => {
            const select = actions.querySelector('select').getBoundingClientRect();
            const button = actions.querySelector('button').getBoundingClientRect();
            return Math.abs(select.top - button.top) < 2;
        }), true, '窄屏看板操作也应保持同一行。');
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
