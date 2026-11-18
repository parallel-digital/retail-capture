import unittest
from page_data import _money, _number

class TestParsing(unittest.TestCase):
    def test_money(self):
        self.assertEqual(_money('$29.99'), 29.99)
        self.assertEqual(_money('$1,249.00'), 1249.0)
        self.assertIsNone(_money(None))
    def test_number(self):
        self.assertEqual(_number('4.6 out of 5 stars'), 4.6)
        self.assertEqual(_number('1,248'), 1248.0)

if __name__ == '__main__':
    unittest.main()
