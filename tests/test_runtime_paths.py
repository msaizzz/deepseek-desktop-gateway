from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.deepseek_gateway import runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def test_docs_dir_uses_project_docs_in_source_mode(self) -> None:
        expected = runtime_paths.项目根目录() / "docs"

        with patch("src.deepseek_gateway.runtime_paths.是否已打包", return_value=False):
            actual = runtime_paths.文档目录()

        self.assertEqual(actual, expected)

    def test_docs_dir_uses_executable_dir_in_packaged_mode(self) -> None:
        executable_dir = Path("D:/fake-release")

        with patch("src.deepseek_gateway.runtime_paths.是否已打包", return_value=True):
            with patch("src.deepseek_gateway.runtime_paths.可执行文件目录", return_value=executable_dir):
                actual = runtime_paths.文档目录()

        self.assertEqual(actual, executable_dir)

    def test_user_data_dir_defaults_to_cwd_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir) / runtime_paths.应用目录名

            with patch("src.deepseek_gateway.runtime_paths.Path.cwd", return_value=Path(temp_dir)):
                with patch.dict("os.environ", {}, clear=False):
                    actual = runtime_paths.用户数据目录()

            self.assertEqual(actual, expected)
            self.assertTrue(actual.exists())

    def test_user_data_dir_honors_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "custom-data"

            with patch.dict("os.environ", {"SRW_GATEWAY_DATA_DIR": str(override)}, clear=False):
                actual = runtime_paths.用户数据目录()

            self.assertEqual(actual, override)
            self.assertTrue(actual.exists())


if __name__ == "__main__":
    unittest.main()