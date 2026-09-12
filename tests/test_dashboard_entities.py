import unittest
from dashboard_entities import task, normalize

class EntityTests(unittest.TestCase):
    def test_task_projection_is_stable(self):
        x = task({'id':'1','title':'  test ','priority':None,'tags':['b','a','a'],'content':None})
        self.assertEqual(x['title'], 'test'); self.assertEqual(x['tags'], ['a','b'])
        self.assertFalse(x['archived']); self.assertEqual(x['content'], '')
    def test_archive_is_derived_from_tag(self):
        self.assertTrue(task({'id':'1','title':'x','tags':['archived']})['archived'])

if __name__ == '__main__': unittest.main()
