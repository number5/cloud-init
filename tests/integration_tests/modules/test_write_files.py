"""Integration test for the write_files module.

This test specifies files to be created by the ``write_files`` module
and then checks if those files were created during boot.

(This is ported from
``tests/cloud_tests/testcases/modules/write_files.yaml``.)"""

import base64

import pytest

ASCII_TEXT = "ASCII text"
B64_CONTENT = base64.b64encode(ASCII_TEXT.encode("utf-8"))

# NOTE: the binary data can be any binary data, not only executables
#       and can be generated via the base 64 command as such:
#           $ base64 < hello > hello.txt
#       the opposite is running:
#           $ base64 -d < hello.txt > hello
#
USER_DATA = """\
#cloud-config
users:
-   default
-   name: myuser
write_files:
-   encoding: b64
    content: {}
    owner: root:root
    path: /root/file_b64
    permissions: '0644'
-   content: |
        # My new /root/file_text

        SMBDOPTIONS="-D"
    path: /root/file_text
-   content: !!binary |
        /Z/xrHR4WINT0UNoKPQKbuovp6+Js+JK
    path: /root/file_binary
    permissions: '0555'
-   encoding: gzip
    content: !!binary |
        H4sIAIDb/U8C/1NW1E/KzNMvzuBKTc7IV8hIzcnJVyjPL8pJ4QIA6N+MVxsAAAA=
    path: /root/file_gzip
    permissions: '0755'
-   path: '/home/testuser/my-file'
    content: |
      echo 'hello world!'
    defer: true
    owner: 'myuser'
    permissions: '0644'
    append: true
-   path: '/home/testuser/subdir1/subdir2/my-file'
    content: |
      echo 'hello world!'
    defer: true
    owner: 'myuser'
    permissions: '0644'
    append: true
""".format(
    B64_CONTENT.decode("ascii")
)


@pytest.mark.ci
@pytest.mark.user_data(USER_DATA)
class TestWriteFiles:
    @pytest.mark.parametrize(
        "cmd,expected_out",
        (
            ("md5sum </root/file_b64", "84baab0d01c1374924dcedfb5972697c"),
            ("md5sum </root/file_binary", "3801184b97bb8c6e63fa0e1eae2920d7"),
            (
                "sha256sum </root/file_binary",
                "2c791c4037ea5bd7e928d6a87380f8ba"
                "7a803cd83d5e4f269e28f5090f0f2c9a",
            ),
            (
                "md5sum </root/file_gzip",
                "ec96d4a61ed762f0ff3725e1140661de",
            ),
            ("md5sum </root/file_text", "a2b6d22fa3d7aa551e22bb0c8acd9121"),
        ),
    )
    def test_write_files(self, cmd, expected_out, class_client):
        out = class_client.execute(cmd)
        assert expected_out in out

    def test_write_files_deferred(self, class_client):
        """Test that write files deferred works as expected.

        Users get created after write_files module runs, so ensure that
        with `defer: true`, the file gets written with correct ownership.
        """
        out = class_client.read_from_file("/home/testuser/my-file")
        assert "echo 'hello world!'" == out
        assert (
            class_client.execute('stat -c "%U %a" /home/testuser/my-file')
            == "myuser 644"
        )
        # Assert write_files per-instance is honored and run only once.
        # Given append: true multiple runs across would append new content.
        class_client.restart()
        out = class_client.read_from_file("/home/testuser/my-file")
        assert "echo 'hello world!'" == out

    def test_write_files_deferred_with_newly_created_dir(self, class_client):
        """Test that newly created directory works as expected.

        Users get created after write_files module runs, so ensure that
        with `defer: true`, the file and directories gets written with correct
        ownership.
        """
        out = class_client.read_from_file(
            "/home/testuser/subdir1/subdir2/my-file"
        )
        assert "echo 'hello world!'" == out
        assert (
            class_client.execute(
                'stat -c "%U %a" /home/testuser/subdir1/subdir2'
            )
            == "myuser 755"
        )
        assert (
            class_client.execute('stat -c "%U %a" /home/testuser/subdir1')
            == "myuser 755"
        )
