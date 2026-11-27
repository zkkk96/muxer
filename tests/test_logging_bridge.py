"""界面日志缓冲的基本行为。"""

from __future__ import annotations

import logging
import unittest

from PySide6.QtCore import QCoreApplication

from muxer.ui.logging_bridge import LogStore


class LogStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_buffer_cap_and_clear(self) -> None:
        store = LogStore(max_lines=3)
        logger = logging.getLogger("muxer.tests.logging")
        logger.setLevel(logging.INFO)
        try:
            for index in range(5):
                logger.info("line-%d", index)
            text = store.text()
            self.assertIn("line-4", text)
            self.assertNotIn("line-0", text)
            self.assertEqual(store.line_count(), 3)
            store.clear()
            self.assertEqual(store.text(), "")
        finally:
            store.close()

    def test_export_text(self) -> None:
        store = LogStore(max_lines=10)
        logging.getLogger("muxer.tests.export").warning("export-me")
        self.assertIn("export-me", store.text())
        store.close()


if __name__ == "__main__":
    unittest.main()
