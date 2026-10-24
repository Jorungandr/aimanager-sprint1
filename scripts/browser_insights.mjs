/**
 * Sprint 3 浏览器回归：数据概览的统计口径、图表下钻、变更记录与三档宽度布局。
 * 使用隔离的临时数据库，不接触真实账号与任务。
 */
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
    temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), 'aimanager-insights-'));
    const database = path.join(temporaryDirectory, 'insights.db');
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
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.once('exit', code => {
        if (code && !failures.some(item => item.name === '测试服务器')) {
            failures.push({ name: '测试服务器', error: new Error(`退出码 ${code}\n${stderr}`) });
        }
    });
    const baseURL = `http://127.0.0.1:${port}`;
    await waitForServer(baseURL, child);
    return { child, baseURL };
}

async function api(context, baseURL, method, resource, data) {
    const headers = {};
    if (['POST', 'PUT', 'DELETE'].includes(method)) {
        const bootstrap = await context.request.get(`${baseURL}/api/csrf`);
        assert.equal(bootstrap.status(), 200);
        headers['X-CSRF-Token'] = (await bootstrap.json()).csrf_token;
    }
    const response = await context.request.fetch(`${baseURL}${resource}`, { method, data, headers });
    let body;
    try { body = await response.json(); } catch (_) { body = null; }
    return { status: response.status(), body };
}

async function expectApi(context, baseURL, method, resource, data, expectedStatus) {
    const result = await api(context, baseURL, method, resource, data);
    assert.equal(result.status, expectedStatus,
        `${method} ${resource} 应返回 ${expectedStatus}，实际为 ${result.status}：${JSON.stringify(result.body)}`);
    return result.body;
}

function dayOffset(days) {
    const target = new Date();
    target.setDate(target.getDate() + days);
    return target.toISOString().slice(0, 10);
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

async function bootstrap(context, page, baseURL) {
    await page.goto('/');
    await page.locator('#setupPanel').waitFor({ state: 'visible' });
    await page.locator('#setupForm input[name="email"]').fill('admin@example.test');
    await page.locator('#setupForm input[name="password"]').fill('AdminPass123');
    await page.locator('#setupForm button').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });

    const viewer = await expectApi(context, baseURL, 'POST', '/api/users',
        { email: 'viewer@example.test', password: 'ViewerPass123' }, 201);
    const role = await expectApi(context, baseURL, 'POST', '/api/roles', { name: '只读成员' }, 201);
    await expectApi(context, baseURL, 'PUT', `/api/roles/${role.id}/permissions`,
        { permissions: ['project.view'] }, 200);
    const project = await expectApi(context, baseURL, 'POST', '/api/projects', { name: '凤凰统计项目' }, 201);
    await expectApi(context, baseURL, 'POST', `/api/projects/${project.id}/members`,
        { user_id: viewer.id, role_id: role.id }, 201);
    const requirement = await expectApi(context, baseURL, 'POST', `/api/projects/${project.id}/requirements`,
        { title: '统计口径需求', description: '覆盖卡片、图表与下钻' }, 201);
    const base = `/api/projects/${project.id}/tasks`;
    const done = await expectApi(context, baseURL, 'POST', base, {
        title: '已完成任务', requirement_id: requirement.id, assignee_user_id: viewer.id, status: 'done',
    }, 201);
    await expectApi(context, baseURL, 'POST', base, {
        title: '逾期任务', requirement_id: requirement.id, assignee_user_id: viewer.id,
        due_date: dayOffset(-5), status: 'doing',
    }, 201);
    await expectApi(context, baseURL, 'POST', base, {
        title: '临期任务', due_date: dayOffset(1), status: 'todo',
    }, 201);
    await expectApi(context, baseURL, 'POST', base, { title: '待梳理任务', status: 'backlog' }, 201);

    // 让成员角色变更留痕：新增第二个角色并改派
    const secondRole = await expectApi(context, baseURL, 'POST', '/api/roles', { name: '观察者' }, 201);
    await expectApi(context, baseURL, 'PUT', `/api/roles/${secondRole.id}/permissions`,
        { permissions: ['project.view'] }, 200);
    await expectApi(context, baseURL, 'POST', `/api/projects/${project.id}/members`,
        { user_id: viewer.id, role_id: secondRole.id }, 201);

    await page.reload();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.getByRole('heading', { name: project.name }).waitFor();
    return { project, requirement, done, viewer };
}

async function main() {
    const running = await startServer();
    server = running.child;
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ baseURL: running.baseURL });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const fixtures = await bootstrap(context, page, running.baseURL);

    await check('数据概览展示四项健康度且与接口一致', async () => {
        await page.getByRole('button', { name: '数据概览', exact: true }).click();
        await page.locator('#insightsPanel').waitFor({ state: 'visible' });
        await page.locator('#insightScope').filter({ hasText: fixtures.project.name }).waitFor();
        const stats = await expectApi(context, running.baseURL, 'GET',
            `/api/projects/${fixtures.project.id}/stats`, undefined, 200);
        const values = await page.locator('.kpi-value').allTextContents();
        assert.deepEqual(values, [String(stats.total), String(stats.completed),
            String(stats.overdue), String(stats.due_soon)]);
        assert.equal(stats.total, 4, '四项任务应全部计入总数。');
        assert.equal(stats.overdue, 1, '只有一条未完成且过期的任务计入逾期。');
        assert.equal(errors.length, 0, `页面脚本错误：${errors.join('；')}`);
    });

    await check('点击状态柱形下钻到任务明细', async () => {
        const statusCard = page.locator('#insightsPanel .insight-card')
            .filter({ has: page.getByRole('heading', { name: '状态分布' }) });
        const bars = statusCard.locator('.chart-bar');
        assert.equal(await bars.count(), 4, '状态分布应有四根柱形。');
        await bars.nth(3).click({ force: true });
        const drill = page.locator('#insightDrill');
        await drill.getByRole('heading', { name: /已完成 · 1 项/ }).waitFor();
        await drill.getByText(fixtures.done.title, { exact: true }).waitFor();
    });

    await check('趋势数据点可下钻到当天完成的任务', async () => {
        const dots = page.locator('#insightsPanel .chart-dot');
        assert.equal(await dots.count(), 14, '趋势图应覆盖 14 天。');
        await dots.last().click({ force: true });
        await page.locator('#insightDrill').getByRole('heading', { name: /完成的任务 · 1 项/ }).waitFor();
    });

    await check('键盘可用：聚焦柱形后回车触发下钻', async () => {
        const bar = page.locator('#insightsPanel .chart-bar').first();
        await bar.focus();
        await page.keyboard.press('Enter');
        await page.locator('#insightDrill').getByRole('heading', { name: /待梳理 · 1 项/ }).waitFor();
    });

    await check('需求完成率下钻与变更记录筛选', async () => {
        const requirementCard = page.locator('#insightsPanel .insight-card')
            .filter({ has: page.getByRole('heading', { name: '需求完成率' }) });
        await requirementCard.locator('tbody tr').first().getByRole('button', { name: '下钻' }).click();
        await page.locator('#insightDrill').getByRole('heading', { name: /统计口径需求 的任务 · 2 项/ }).waitFor();
        const audit = page.locator('#insightsPanel .audit-list');
        await audit.waitFor();
        assert.ok((await audit.textContent()).includes('@example.test'), '变更记录应包含操作人。');
        await page.getByLabel('按对象类型筛选变更记录').selectOption('membership');
        await page.locator('#insightsPanel .audit-list').filter({ hasText: '角色' }).waitFor();
        const texts = await page.locator('#insightsPanel .audit-list li').allTextContents();
        assert.ok(texts.some(text => text.includes('角色') && text.includes('→')), '角色变更应记录改派前后的值。');
    });

    await check('只读成员可查看数据概览但不能修改', async () => {
        const viewerContext = await browser.newContext({ baseURL: running.baseURL });
        const viewerPage = await viewerContext.newPage();
        await viewerPage.goto('/');
        await viewerPage.locator('#loginPanel').waitFor({ state: 'visible' });
        await viewerPage.locator('#loginForm input[name="email"]').fill('viewer@example.test');
        await viewerPage.locator('#loginForm input[name="password"]').fill('ViewerPass123');
        await viewerPage.locator('#loginForm button').click();
        await viewerPage.locator('#workspace').waitFor({ state: 'visible' });
        await viewerPage.getByRole('button', { name: '数据概览', exact: true }).click();
        await viewerPage.locator('#insightsPanel .kpi-value').first().waitFor();
        assert.equal(await viewerPage.locator('#insightsPanel .workitem-forms').count(), 0, '只读成员不应看到编辑表单。');
        const status = await viewerPage.evaluate(projectId => fetch(`/api/projects/${projectId}/stats`)
            .then(response => response.status), fixtures.project.id);
        assert.equal(status, 200, '只读成员应能读取统计。');
        await viewerContext.close();
    });

    await check('三档宽度下数据概览不产生页面横向溢出', async () => {
        for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
            await page.setViewportSize({ width, height });
            await page.getByRole('button', { name: '数据概览', exact: true }).click();
            await page.locator('#insightsPanel .kpi-value').first().waitFor();
            const auditResult = await page.evaluate(() => {
                const limit = document.documentElement.clientWidth;
                const offenders = [...document.querySelectorAll('body *')]
                    .map(element => ({ element, box: element.getBoundingClientRect() }))
                    .filter(item => item.box.right > limit + 1 && item.box.width > 0)
                    .slice(0, 3)
                    .map(item => `${item.element.tagName.toLowerCase()}.${item.element.className || '-'}右边界${Math.round(item.box.right)}`);
                return { overflow: document.documentElement.scrollWidth - limit, offenders };
            });
            assert.ok(auditResult.overflow <= 1,
                `${width}px 下页面横向溢出 ${auditResult.overflow}px：${auditResult.offenders.join('；')}`);
            if (width > 700) continue;
            const smallTargets = await page.evaluate(() => [...document.querySelectorAll('#insightsPanel button, #insightsPanel select')]
                .filter(element => element.offsetParent !== null)
                .map(element => {
                    const box = element.getBoundingClientRect();
                    return { label: element.textContent.trim().slice(0, 12), width: box.width, height: box.height };
                })
                .filter(item => item.width < 44 || item.height < 44));
            assert.deepEqual(smallTargets, [], `${width}px 下有控件小于 44×44：${JSON.stringify(smallTargets)}`);
        }
    });

    await check('窄屏图表随卡片缩放而不撑开页面', async () => {
        await page.setViewportSize({ width: 390, height: 844 });
        const sizes = await page.locator('#insightsPanel .chart').first().evaluate(element => ({
            chart: element.getBoundingClientRect().width,
            card: element.closest('.insight-card').getBoundingClientRect().width,
        }));
        assert.ok(sizes.chart <= sizes.card, `图表宽度 ${sizes.chart} 超过卡片 ${sizes.card}。`);
    });

    if (browser) await browser.close();
    if (server.exitCode === null) server.kill();
    if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true });

    if (failures.length) {
        console.error(`\n${failures.length} 项未通过。`);
        process.exitCode = 1;
    } else {
        console.log('\n数据概览浏览器回归全部通过。');
    }
}

main().catch(async error => {
    console.error(error);
    if (browser) await browser.close();
    if (server && server.exitCode === null) server.kill();
    if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true });
    process.exitCode = 1;
});
