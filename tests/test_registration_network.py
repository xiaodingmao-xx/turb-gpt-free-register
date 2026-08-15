import unittest

from core.registration_network import extract_public_ip, normalize_public_ip


class RegistrationNetworkTests(unittest.TestCase):
    def test_normalize_public_ip_accepts_ipv4_and_ipv6(self):
        self.assertEqual(normalize_public_ip(" 203.0.113.9 "), "203.0.113.9")
        self.assertEqual(normalize_public_ip("2001:0db8::1"), "2001:db8::1")

    def test_extract_public_ip_reads_common_response_shapes(self):
        self.assertEqual(extract_public_ip({"ip": "203.0.113.10"}), "203.0.113.10")
        self.assertEqual(extract_public_ip({"data": {"query": "198.51.100.4"}}), "198.51.100.4")
        self.assertEqual(extract_public_ip({"ip": "not-an-ip"}), "")


if __name__ == "__main__":
    unittest.main()
