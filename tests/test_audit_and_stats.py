"""Sprint 3 回归：变更事件、项目统计、风险清单、下钻与 v3 迁移。"""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service import AppError, Store
import database


class AuditEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'audit.db')
        self.store = Store(self.path)
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')
        self.project = self.store.create_project('Project')
        self.member = self.store.create_user('member@example.test', 'MemberPass123')
        self.role = self.store.create_role('成员')
        self.store.add_member(self.project['id'], self.member['id'], self.role['id'], actor=self.admin)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def task(self, title='Task', status='todo', due=None):
        return self.store.create_task(self.project['id'], title, None, self.member['id'],
                                      due, self.admin['id'], status)

    def test_create_and_status_change_are_recorded(self):
        task = self.task()
        events = self.store.list_audit(self.project['id'], entity_type='task', entity_id=task['id'])
        self.assertEqual([(event['action'], event['field'], event['to_value']) for event in events],
                         [('create', 'status', 'todo')])
        self.assertEqual(events[0]['actor_email'], 'admin@example.test')
        self.store.update_task(self.project['id'], task['id'], {'status': 'doing', 'version': 1},
                               actor=self.member['id'])
        events = self.store.list_audit(self.project['id'], entity_type='task', entity_id=task['id'])
        self.assertEqual(len(events), 2)
        change = events[0]
        self.assertEqual((change['action'], change['field'], change['from_value'], change['to_value']),
                         ('update', 'status', 'todo', 'doing'))
        self.assertEqual(change['actor_email'], 'member@example.test')

    def test_unchanged_update_records_nothing(self):
        task = self.task()
        self.store.update_task(self.project['id'], task['id'], {'title': 'Task', 'version': 1})
        events = self.store.list_audit(self.project['id'], entity_type='task', entity_id=task['id'])
        self.assertEqual(len(events), 1)

    def test_requirement_archive_and_membership_events(self):
        requirement = self.store.create_requirement(self.project['id'], '需求', '说明', self.admin['id'])
        self.store.archive_requirement(self.project['id'], requirement['id'], requirement['version'],
                                       actor=self.admin['id'])
        events = self.store.list_audit(self.project['id'], entity_type='requirement')
        self.assertEqual([(event['action'], event['from_value'], event['to_value']) for event in events],
                         [('archive', '0', '1'), ('create', None, '需求')])
        other = self.store.create_role('观察者')
        self.store.add_member(self.project['id'], self.member['id'], other['id'], actor=self.admin)
        membership = self.store.list_audit(self.project['id'], entity_type='membership')
        self.assertEqual((membership[0]['action'], membership[0]['from_value'], membership[0]['to_value']),
                         ('update', str(self.role['id']), str(other['id'])))
        self.store.remove_member(self.project['id'], self.member['id'], actor=self.admin)
        membership = self.store.list_audit(self.project['id'], entity_type='membership')
        self.assertEqual(membership[0]['action'], 'remove')

    def test_audit_filters_and_bounds(self):
        task = self.task()
        with self.assertRaises(AppError):
            self.store.list_audit(self.project['id'], entity_type='project')
        with self.assertRaises(AppError):
            self.store.list_audit(self.project['id'], limit=101)
        self.assertEqual(self.store.list_audit(self.project['id'], limit=1)[0]['entity_id'], task['id'])
        self.assertEqual(len(self.store.list_audit(self.project['id'], entity_type='membership')), 1)

    def test_events_do_not_leak_across_projects(self):
        other = self.store.create_project('Other')
        foreign = self.store.create_task(other['id'], 'Foreign', None, None, None, self.admin['id'], 'todo')
        self.assertEqual(self.store.list_audit(self.project['id'], entity_type='task'), [])
        self.assertEqual(len(self.store.list_audit(other['id'], entity_type='task')), 1)
        self.assertEqual(self.store.list_audit(other['id'])[0]['entity_id'], foreign['id'])


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'stats.db')
        self.store = Store(self.path)
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')
        self.project = self.store.create_project('Project')
        self.member = self.store.create_user('member@example.test', 'MemberPass123')
        self.role = self.store.create_role('成员')
        self.store.add_member(self.project['id'], self.member['id'], self.role['id'], actor=self.admin)
        self.today = date(2026, 10, 24)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def build(self):
        requirement = self.store.create_requirement(self.project['id'], '需求A', '', self.admin['id'])
        project_id = self.project['id']
        self.done = self.store.create_task(project_id, 'Done', requirement['id'], self.member['id'],
                                           None, self.admin['id'], 'done')
        self.doing = self.store.create_task(project_id, 'Doing', requirement['id'], self.member['id'],
                                            '2026-10-20', self.admin['id'], 'doing')
        self.todo = self.store.create_task(project_id, 'Todo', None, None, '2026-10-25',
                                           self.admin['id'], 'todo')
        self.backlog = self.store.create_task(project_id, 'Backlog', None, None, None,
                                              self.admin['id'], 'backlog')
        # 固定事件日期，使历史趋势断言不依赖测试执行当天或 CI 时区。
        self.store.db.execute('UPDATE audit_events SET created_at=? WHERE project_id=?',
                              (f'{self.today.isoformat()} 12:00:00', project_id))
        self.store.db.commit()
        return requirement

    def test_empty_project_is_zeroed(self):
        stats = self.store.project_stats(self.project['id'], today=self.today)
        self.assertEqual((stats['total'], stats['completed'], stats['overdue']), (0, 0, 0))
        self.assertEqual(stats['completion_rate'], 0.0)
        self.assertEqual(len(stats['trend']), 14)
        self.assertEqual(stats['trend'][-1], {'date': '2026-10-24', 'completed': 0})

    def test_counts_and_distributions(self):
        requirement = self.build()
        stats = self.store.project_stats(self.project['id'], today=self.today)
        self.assertEqual((stats['total'], stats['completed'], stats['overdue'], stats['due_soon']),
                         (4, 1, 1, 1))
        self.assertEqual(stats['completion_rate'], 25.0)
        self.assertEqual({item['status']: item['count'] for item in stats['by_status']},
                         {'backlog': 1, 'todo': 1, 'doing': 1, 'done': 1})
        by_assignee = {item['email']: (item['total'], item['completed']) for item in stats['by_assignee']}
        self.assertEqual(by_assignee['member@example.test'], (2, 1))
        self.assertEqual(by_assignee['未指派'], (2, 0))
        by_requirement = {item['requirement_id']: (item['total'], item['completed'])
                          for item in stats['by_requirement']}
        self.assertEqual(by_requirement[requirement['id']], (2, 1))
        self.assertEqual(by_requirement[None], (2, 0))

    def test_trend_uses_first_completion_day_and_legacy_baseline(self):
        self.build()
        stats = self.store.project_stats(self.project['id'], days=7, today=self.today)
        self.assertEqual(len(stats['trend']), 7)
        self.assertEqual(stats['trend'][-1]['completed'], 1)
        self.assertEqual(stats['trend'][0]['date'], '2026-10-18')
        # 历史完成但无事件的任务计入窗口起点基线，保证末点与完成数一致
        self.store.db.execute('''INSERT INTO tasks(project_id,title,status,created_by)
            VALUES(?,?,'done',?)''', (self.project['id'], 'Legacy', self.admin['id']))
        self.store.db.commit()
        stats = self.store.project_stats(self.project['id'], days=7, today=self.today)
        self.assertEqual(stats['completed'], 2)
        self.assertEqual(stats['trend'][0]['completed'], 1)
        self.assertEqual(stats['trend'][-1]['completed'], 2)

    def test_invalid_stats_parameters(self):
        for value in ('2026-13-01', 'today', 20261024):
            with self.assertRaises(AppError):
                self.store.project_stats(self.project['id'], today=value)
        with self.assertRaises(AppError):
            self.store.project_stats(self.project['id'], days=91)
        with self.assertRaises(AppError):
            self.store.project_stats(999, today=self.today)

    def test_risk_list_marks_overdue_and_due_soon(self):
        self.build()
        near = self.store.create_task(self.project['id'], 'Near', None, None, '2026-10-26',
                                      self.admin['id'], 'todo')
        risks = self.store.project_risks(self.project['id'], today=self.today)
        levels = {item['id']: (item['level'], item['days_left']) for item in risks['items']}
        self.assertEqual(levels[self.doing['id']], ('overdue', -4))
        self.assertEqual(levels[self.todo['id']], ('due_soon', 1))
        self.assertIn(near['id'], levels)
        self.assertEqual((risks['overdue'], risks['due_soon']), (1, 2))
        far = self.store.create_task(self.project['id'], 'Far', None, None, '2026-11-30',
                                     self.admin['id'], 'todo')
        self.assertNotIn(far['id'], {item['id'] for item in self.store.project_risks(
            self.project['id'], today=self.today)['items']})

    def test_drilldown_dimensions(self):
        requirement = self.build()
        project_id = self.project['id']
        by_status = self.store.drilldown_tasks(project_id, 'status', 'doing')
        self.assertEqual([item['title'] for item in by_status['items']], ['Doing'])
        by_assignee = self.store.drilldown_tasks(project_id, 'assignee', str(self.member['id']))
        self.assertEqual(by_assignee['total'], 2)
        self.assertEqual(self.store.drilldown_tasks(project_id, 'assignee', 'unassigned')['total'], 2)
        by_requirement = self.store.drilldown_tasks(project_id, 'requirement', str(requirement['id']))
        self.assertEqual(by_requirement['total'], 2)
        self.assertEqual(self.store.drilldown_tasks(project_id, 'requirement', 'none')['total'], 2)
        completed = self.store.drilldown_tasks(project_id, 'completed_on', self.today.isoformat())
        self.assertEqual([item['title'] for item in completed['items']], ['Done'])
        with self.assertRaises(AppError):
            self.store.drilldown_tasks(project_id, 'owner', '1')
        with self.assertRaises(AppError):
            self.store.drilldown_tasks(project_id, 'status', 'unknown')
        with self.assertRaises(AppError):
            self.store.drilldown_tasks(project_id, 'status', 'done', limit=201)

    def test_drilldown_is_project_scoped(self):
        self.build()
        other = self.store.create_project('Other')
        self.store.create_task(other['id'], 'Foreign', None, None, None, self.admin['id'], 'todo')
        self.assertEqual(self.store.drilldown_tasks(self.project['id'], 'status', 'todo')['total'], 1)
        self.assertEqual(self.store.drilldown_tasks(other['id'], 'status', 'todo')['total'], 1)

    def test_drilldown_total_is_not_the_page_size(self):
        self.build()
        result = self.store.drilldown_tasks(self.project['id'], 'assignee', 'unassigned', limit=1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['truncated'])

    def test_drilldown_exact_limit_is_not_truncated(self):
        self.build()
        result = self.store.drilldown_tasks(self.project['id'], 'status', 'todo', limit=1)
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['truncated'])

    def test_drilldown_empty_result_is_not_truncated(self):
        result = self.store.drilldown_tasks(self.project['id'], 'status', 'todo', limit=1)
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['items'], [])
        self.assertFalse(result['truncated'])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'migrate.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_v2_database_gains_audit_table_and_backup(self):
        store = Store(self.path)
        admin = store.setup('admin@example.test', 'AdminPass123')
        project = store.create_project('Project')
        store.create_task(project['id'], 'Existing', None, None, None, admin['id'], 'todo')
        store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE audit_events')
            db.execute('PRAGMA user_version=2')
            db.commit()
        upgraded = Store(self.path)
        try:
            self.assertEqual(upgraded.db.execute('PRAGMA user_version').fetchone()[0], database.SCHEMA_VERSION)
            self.assertTrue((Path(self.path).parent / 'migrate.db.pre-audit.bak').exists())
            # 旧任务没有事件，统计仍能给出完整口径
            stats = upgraded.project_stats(project['id'], today=date(2026, 10, 24))
            self.assertEqual(stats['total'], 1)
            self.assertEqual(upgraded.list_audit(project['id']), [])
            task = upgraded.list_tasks(project['id'])[0]
            upgraded.update_task(project['id'], task['id'], {'status': 'done', 'version': 1})
            self.assertEqual(len(upgraded.list_audit(project['id'], entity_type='task')), 1)
        finally:
            upgraded.close()

    def test_newer_schema_is_rejected(self):
        store = Store(self.path)
        store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(f'PRAGMA user_version={database.SCHEMA_VERSION + 1}')
            db.commit()
        with self.assertRaises(AppError) as raised:
            Store(self.path)
        self.assertEqual(raised.exception.status, 409)

    def test_audit_table_is_created_on_memory_database(self):
        store = Store(':memory:')
        try:
            tables = {row['name'] for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('audit_events', tables)
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], database.SCHEMA_VERSION)
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
