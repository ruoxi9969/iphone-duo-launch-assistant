"""Apple 官方自提响应解析与 7 态模型单元测试"""

import unittest
from src.pickup_monitor import parse_fulfillment_response, InventoryStatus

class TestPickupParser(unittest.TestCase):

    def setUp(self):
        self.store_id = "R390"
        self.part_no = "MG734CH/A"

    def test_parse_available(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "MG734CH/A": {
                                    "pickupDisplay": "available",
                                    "pickupSearchQuote": "今天可取货",
                                    "messageTypes": {
                                        "regular": {
                                            "storePickupQuote": "今天可取货",
                                            "storePickupProductTitle": "iPhone 17 512GB 白色"
                                        }
                                    }
                                }
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.AVAILABLE)
        self.assertEqual(res.store_name, "香港广场")
        self.assertEqual(res.pickup_quote, "今天可取货")
        self.assertEqual(res.product_title, "iPhone 17 512GB 白色")

    def test_parse_unavailable(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "MG734CH/A": {
                                    "pickupDisplay": "unavailable",
                                    "pickupSearchQuote": "暂无现货",
                                }
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.UNAVAILABLE)
        self.assertEqual(res.pickup_quote, "暂无现货")

    def test_parse_ineligible(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "MG734CH/A": {
                                    "pickupDisplay": "ineligible",
                                }
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.NOT_FOR_PICKUP)

    def test_parse_coming_soon(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "MG734CH/A": {
                                    "pickupDisplay": "coming_soon",
                                }
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.COMING_SOON)

    def test_parse_not_yet_released(self):
        data = {
            "body": {
                "content": {
                    "deliveryMessage": {
                        "MG734CH/A": {
                            "regular": {
                                "buyability": {
                                    "reason": "NOT_FOR_SALE"
                                }
                            }
                        }
                    },
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "MG734CH/A": {
                                    "pickupDisplay": "unavailable",
                                }
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.NOT_YET_RELEASED)

    def test_empty_stores_is_unknown_not_unavailable(self):
        data = {"body": {"content": {"pickupMessage": {"stores": []}}}}
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.UNKNOWN)
        self.assertNotEqual(res.status, InventoryStatus.UNAVAILABLE)
        self.assertIn("NoPickupData", res.reason)

    def test_store_not_returned_is_unknown_not_unavailable(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R999",
                            "storeName": "其他门店",
                            "partsAvailability": {}
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.UNKNOWN)
        self.assertNotEqual(res.status, InventoryStatus.UNAVAILABLE)
        self.assertIn("StoreNotReturned", res.reason)

    def test_part_not_returned_is_unknown_not_unavailable(self):
        data = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [{
                            "storeNumber": "R390",
                            "storeName": "香港广场",
                            "partsAvailability": {
                                "OTHER_SKU": {"pickupDisplay": "available"}
                            }
                        }]
                    }
                }
            }
        }
        res = parse_fulfillment_response(data, self.store_id, self.part_no)
        self.assertEqual(res.status, InventoryStatus.UNKNOWN)
        self.assertNotEqual(res.status, InventoryStatus.UNAVAILABLE)
        self.assertIn("PartNotReturned", res.reason)

if __name__ == "__main__":
    unittest.main()
