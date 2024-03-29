# This file is part of cloud-init. See LICENSE file for license information.

import json
import os
import stat

from cloudinit import atomic_helper
from tests.unittests.helpers import CiTestCase


class TestAtomicHelper(CiTestCase):
    def test_basic_usage(self):
        """write_file takes bytes if no omode."""
        path = self.tmp_path("test_basic_usage")
        contents = b"Hey there\n"
        atomic_helper.write_file(path, contents)
        self.check_file(path, contents)

    def test_string(self):
        """write_file can take a string with mode w."""
        path = self.tmp_path("test_string")
        contents = "Hey there\n"
        atomic_helper.write_file(path, contents, omode="w")
        self.check_file(path, contents, omode="r")

    def test_file_permissions(self):
        """write_file with mode 400 works correctly."""
        path = self.tmp_path("test_file_permissions")
        contents = b"test_file_perms"
        atomic_helper.write_file(path, contents, mode=0o400)
        self.check_file(path, contents, perms=0o400)

    def test_file_preserve_permissions(self):
        """create a file with mode 700, then write_file with mode 644."""
        path = self.tmp_path("test_file_preserve_permissions")
        contents = b"test_file_perms"
        with open(path, mode="wb") as f:
            f.write(b"test file preserve permissions")
            os.chmod(f.name, 0o700)
            atomic_helper.write_file(path, contents, preserve_mode=True)
            self.check_file(path, contents, perms=0o700)

    def test_write_json(self):
        """write_json output is readable json."""
        path = self.tmp_path("test_write_json")
        data = {"key1": "value1", "key2": ["i1", "i2"]}
        atomic_helper.write_json(path, data)
        with open(path, "r") as fp:
            found = json.load(fp)
        self.assertEqual(data, found)
        self.check_perms(path, 0o644)

    def check_file(self, path, content, omode=None, perms=0o644):
        if omode is None:
            omode = "rb"
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.isfile(path))
        with open(path, omode) as fp:
            found = fp.read()
            self.assertEqual(content, found)
        self.check_perms(path, perms)

    def check_perms(self, path, perms):
        file_stat = os.stat(path)
        self.assertEqual(perms, stat.S_IMODE(file_stat.st_mode))

    def test_write_file_ensure_dirs(self):
        path = self.tmp_path("ensure_dirs") + "/ensure/dir"
        contents = b"Hey there\n"
        atomic_helper.write_file(path, contents)
        self.check_file(path, contents)
