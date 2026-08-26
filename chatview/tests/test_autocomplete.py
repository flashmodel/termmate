import os
import unittest
import tempfile
import shutil
from chatview.autocomplete import parse_at_query_text, AutoComplete


class TestAutocomplete(unittest.TestCase):

    def test_parse_at_query_simple(self):
        # "@" -> full_query="", dir_part="", file_filter=""
        parsed = parse_at_query_text("Please check @")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "")
        self.assertEqual(dir_part, "")
        self.assertEqual(file_filter, "")
        self.assertEqual(offset, 13)

    def test_parse_at_query_directory(self):
        # "@src/" -> full_query="src/", dir_part="src/", file_filter=""
        parsed = parse_at_query_text("Please check @src/")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/")
        self.assertEqual(dir_part, "src/")
        self.assertEqual(file_filter, "")

    def test_parse_at_query_nested_with_filter(self):
        # "@src/components/Cha" -> full_query="src/components/Cha", dir_part="src/components/", file_filter="Cha"
        parsed = parse_at_query_text("Check @src/components/Cha")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/components/Cha")
        self.assertEqual(dir_part, "src/components/")
        self.assertEqual(file_filter, "Cha")

    def test_parse_at_query_space_breaks_token(self):
        # Space after @ without escaping -> None
        parsed = parse_at_query_text("Check @src/components something ")
        self.assertIsNone(parsed)

    def test_parse_at_query_escaped_space(self):
        # Escaped space in path -> allowed
        parsed = parse_at_query_text(r"Check @my\ folder/comp")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, r"my\ folder/comp")

    def test_multi_workspace_target_dir_routing(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            cwd_dir = os.path.join(base_tmp, "frontend")
            other_ws_dir = os.path.join(base_tmp, "backend")
            os.makedirs(os.path.join(cwd_dir, "src", "components"), exist_ok=True)
            os.makedirs(os.path.join(other_ws_dir, "cmd", "server"), exist_ok=True)

            other_workspaces = [other_ws_dir]

            # 1. CWD root
            resolved_cwd_root = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "")
            self.assertEqual(resolved_cwd_root, cwd_dir)

            # 2. CWD subdirectory (e.g. "@src/components/")
            resolved_cwd_sub = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "src/components/")
            self.assertEqual(resolved_cwd_sub, os.path.join(cwd_dir, "src", "components"))

            # 3. Other workspace root (e.g. "@backend/")
            resolved_other_root = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "backend/")
            self.assertEqual(resolved_other_root, other_ws_dir)

            # 4. Other workspace subdirectory (e.g. "@backend/cmd/server/")
            resolved_other_sub = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "backend/cmd/server/")
            self.assertEqual(resolved_other_sub, os.path.join(other_ws_dir, "cmd", "server"))

            # 5. Sandbox boundary protection (traversal outside project)
            invalid_traversal = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "../../etc/")
            self.assertIsNone(invalid_traversal)

            invalid_other_traversal = AutoComplete.resolve_target_dir(cwd_dir, other_workspaces, "backend/../../etc/")
            self.assertIsNone(invalid_other_traversal)

    def test_scan_directory_and_sorting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "components"), exist_ok=True)
            os.makedirs(os.path.join(tmpdir, "hooks"), exist_ok=True)
            os.makedirs(os.path.join(tmpdir, "node_modules"), exist_ok=True)  # ignored
            os.makedirs(os.path.join(tmpdir, ".git"), exist_ok=True)          # ignored

            with open(os.path.join(tmpdir, "index.ts"), "w") as f:
                f.write("")
            with open(os.path.join(tmpdir, "main.ts"), "w") as f:
                f.write("")
            with open(os.path.join(tmpdir, ".DS_Store"), "w") as f:           # ignored
                f.write("")

            sub_dirs, sub_files = AutoComplete.scan_directory(tmpdir, "")
            self.assertEqual(sub_dirs, ["components", "hooks"])
            self.assertEqual(sub_files, ["index.ts", "main.ts"])

            # Filtered scan
            sub_dirs_f, sub_files_f = AutoComplete.scan_directory(tmpdir, "co")
            self.assertEqual(sub_dirs_f, ["components"])
            self.assertEqual(sub_files_f, [])


if __name__ == "__main__":
    unittest.main()
