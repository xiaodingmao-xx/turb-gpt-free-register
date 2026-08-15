# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import plan_check_service


class PlanCheckServiceTests(unittest.TestCase):
    def test_plan_log_path_and_append_log_keep_email_safe(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(plan_check_service, "_LOG_DIR", Path(td)):
                plan_check_service._append_log("person/a@example.com", "[Plan] test", clear=True)
                path = plan_check_service.log_path("person/a@example.com")
                self.assertEqual(path.name, "plan-check-person_a@example.com.log")
                self.assertIn("[Plan] test", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
