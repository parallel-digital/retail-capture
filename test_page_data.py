import unittest
from page_data import _brand_from_title, _enrich_result, _money, _number, _page_key


class TestParsing(unittest.TestCase):
    def test_money(self):
        self.assertEqual(_money('$29.99'), 29.99)
        self.assertEqual(_money('$1,249.00'), 1249.0)
        self.assertIsNone(_money(None))

    def test_number(self):
        self.assertEqual(_number('4.6 out of 5 stars'), 4.6)
        self.assertEqual(_number('1,248'), 1248.0)

    def test_deal_discount_and_result_key(self):
        row = {
            'account': 'REEKON', 'retailer': 'Amazon', 'page_type': 'deal_page',
            'name': 'Black Friday Tools', 'url': 'https://www.amazon.com/blackfriday'
        }
        item = {
            'deal_rank': 1, 'position': 1, 'asin': 'B012345678',
            'title': 'Cobra Test Product', 'price': 79.99, 'list_price': 99.99,
            'visible_text': 'Black Friday Deal 4.5 out of 5 stars (1.2K)'
        }
        enriched = _enrich_result(item, row, _page_key(row))
        self.assertEqual(enriched['brand'], 'Cobra')
        self.assertEqual(enriched['discount_pct'], 20.0)
        self.assertEqual(enriched['rating'], 4.5)
        self.assertEqual(enriched['review_count'], 1200)
        self.assertTrue(enriched['result_key'].endswith('|b012345678'))
        self.assertIn('Black Friday Deal', enriched['badges'])

    def test_unknown_brand_stays_null(self):
        self.assertIsNone(_brand_from_title('Unknown Maker Product'))


if __name__ == '__main__':
    unittest.main()
