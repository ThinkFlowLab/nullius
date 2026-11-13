"""Hosts: the two spellings of an entry, the ssh config scan that reads
names and never keys, and the connection test that sends one probe over
ssh in BatchMode and reads what the host answers."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nullius import hosts, settings


class Entries(unittest.TestCase):
    def test_an_alias_and_a_manual_entry(self):
        self.assertEqual((hosts.parse("bench").kind, hosts.parse("bench").label), ("alias", "bench"))
        h = hosts.parse("ubuntu@10.0.0.4:2222:~/.ssh/id_bench")
        self.assertEqual((h.kind, h.user, h.host, h.port, h.key, h.label), ("manual", "ubuntu", "10.0.0.4", 2222, "~/.ssh/id_bench", "ubuntu@10.0.0.4:2222"))
        self.assertEqual(hosts.parse("ubuntu@[fe80::1]:22").host, "[fe80::1]")
        for bad in ("", "two words", "user@", "@host", "ubuntu@h:0", "ubuntu@h:70000", "ubuntu@h:22:relative/key", "a;b"):
            with self.assertRaises(ValueError, msg=bad):
                hosts.parse(bad)

    def test_named_manual_connection_keeps_ssh_target_separate(self):
        entry = "GPU lab=ubuntu@192.0.2.10:2222:~/.ssh/lab"
        host = hosts.parse(entry)
        self.assertEqual(host.entry, entry)
        self.assertEqual(host.label, "GPU lab")
        self.assertEqual(host.to_dict()["label"], "GPU lab")
        self.assertEqual(host.argv("true")[-2:], ["ubuntu@192.0.2.10", "true"])
        self.assertEqual(hosts.parse(host.entry), host)
        self.assertIsNone(hosts.validate_list(entry + ", bench"))
        for bad in ("=u@h", "a" * 65 + "=u@h", "bad\nname=u@h", "Name=alias", "a,b=u@h"):
            with self.assertRaises(ValueError):
                hosts.parse(bad)

    def test_the_list_setting(self):
        self.assertEqual([h.entry for h in hosts.parse_list(" bench, ubuntu@10.0.0.4 ,, bench")], ["bench", "ubuntu@10.0.0.4"])
        self.assertIsNone(settings.BY_NAME["HOSTS"].validate("bench, ubuntu@10.0.0.4:22"))
        self.assertIn("not a host", settings.BY_NAME["HOSTS"].validate("bench, not a host"))

    def test_an_entry_is_changed_in_its_place_or_taken_out(self):
        self.assertEqual(hosts.replace("bench, u@h:22", "u@h:22", "Lab=u@h:22:~/.ssh/k")[0], "bench, Lab=u@h:22:~/.ssh/k")
        self.assertEqual(hosts.replace("bench, u@h:22", "bench", None), ("u@h:22", None))
        for old, new in (("u@h:22", "bench"), ("gone", "lab"), ("bench", "not a host")):
            with self.assertRaises(ValueError, msg=(old, new)):
                hosts.replace("bench, u@h:22", old, new)
        self.assertTrue(hosts.same_machine(hosts.parse("u@h:22"), hosts.parse("Lab=u@h:22:~/.ssh/k")))
        self.assertFalse(hosts.same_machine(hosts.parse("u@h:22"), hosts.parse("u@other:22")))

    def test_the_ssh_argv_prompts_for_nothing(self):
        argv = hosts.parse("ubuntu@10.0.0.4:2222:~/.ssh/id_bench").argv("true")
        self.assertIn("BatchMode=yes", argv); self.assertEqual(argv[-2:], ["ubuntu@10.0.0.4", "true"])
        self.assertEqual(argv[argv.index("-p") + 1], "2222"); self.assertEqual(argv[argv.index("-i") + 1], os.path.expanduser("~/.ssh/id_bench"))
        self.assertIn("IdentitiesOnly=yes", argv)            # the named key, not each of the agent's first
        alias = hosts.parse("bench").argv("true")
        self.assertNotIn("IdentitiesOnly=yes", alias)        # an alias: ~/.ssh/config decides
        self.assertNotIn("-i", alias); self.assertNotIn("-p", alias); self.assertEqual(alias[-2:], ["bench", "true"])


class SshConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-hosts-")); self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_aliases_with_what_the_config_says_and_no_key(self):
        cfg = self.tmp / "config"
        cfg.write_text("# machines\nHost bench\n  HostName 10.0.0.4\n  User ubuntu\n  IdentityFile ~/.ssh/id_bench\n  Port 22\n\nHost *.internal gpu-?\n  User root\n\nHost lab\n    User=me\nMatch host x\n  User other\nHost a b\n  HostName shared\n")
        scanned = hosts.scan_ssh_config(str(cfg))
        self.assertEqual([h["alias"] for h in scanned], ["bench", "lab", "a", "b"])       # patterns are not machines
        self.assertEqual(scanned[0], {"alias": "bench", "hostname": "10.0.0.4", "user": "ubuntu", "port": "22"})
        self.assertEqual(scanned[1]["user"], "me"); self.assertIsNone(scanned[1]["hostname"])   # a Match block does not leak into lab
        self.assertEqual([h["hostname"] for h in scanned[2:]], ["shared", "shared"])
        self.assertNotIn("id_bench", repr(scanned))

    def test_no_config_is_no_hosts(self):
        self.assertEqual(hosts.scan_ssh_config(str(self.tmp / "absent")), [])


class ConnectionTest(unittest.TestCase):
    def test_what_the_host_answers(self):
        seen = []
        def run(argv, timeout):
            seen.append(argv)
            return 0, "ok\nuv=yes\ndocker=yes\npodman=yes\nharbor=yes\nharbor_version=harbor 0.22.0+agtai.1\ndocker_ok=yes\npodman_ok=no\nhostname=ecs-bench\n", ""
        r = hosts.test(hosts.parse("bench"), run=run)
        self.assertTrue(r["ok"]); self.assertEqual((r["uv"], r["docker"], r["docker_ok"], r["harbor"]), (True, True, True, True))
        self.assertEqual((r["podman"], r["podman_ok"], r["engine"]), (True, False, "docker"))
        self.assertEqual((r["harbor_version"], r["hostname"], r["error"]), ("0.22.0+agtai.1", "ecs-bench", None))
        self.assertEqual(seen[0][-1], hosts.PROBE); self.assertIn("BatchMode=yes", seen[0])   # one command, no prompt possible
        self.assertFalse(r["docker_userns"])

    def test_the_probe_says_when_the_host_s_docker_has_userns_remap(self):
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        for options, said in (("[name=seccomp,profile=builtin name=userns name=cgroupns]", True), ("[name=seccomp,profile=builtin]", False)):
            with open(os.path.join(tmp, "docker"), "w") as f:
                f.write(f"#!/bin/sh\n[ \"$1\" = info ] && echo '{options}'\nexit 0\n")
            os.chmod(os.path.join(tmp, "docker"), 0o755)
            env = {"PATH": tmp + os.pathsep + "/usr/bin:/bin"}
            run = lambda argv, timeout: (lambda p: (p.returncode, p.stdout, p.stderr))(
                subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True, env=env, timeout=timeout))
            with self.subTest(options=options):
                self.assertEqual(hosts.test(hosts.parse("bench"), run=run)["docker_userns"], said)

    def test_missing_tools_are_said_not_failed(self):
        r = hosts.test(hosts.parse("bench"), run=lambda a, t: (0, "ok\nuv=no\ndocker=no\npodman=no\nharbor=no\nhostname=x\n", ""))
        self.assertTrue(r["ok"]); self.assertEqual((r["uv"], r["docker"], r["harbor"], r["harbor_version"]), (False, False, False, None))
        self.assertEqual((r["podman"], r["podman_ok"], r["engine"]), (False, None, None))
        r = hosts.test(hosts.parse("bench"), run=lambda a, t: (0, "ok\ndocker=no\npodman=yes\npodman_ok=yes\n", ""))
        self.assertEqual(r["engine"], "podman")

    def test_the_probe_asks_each_engine_that_is_there(self):
        with tempfile.TemporaryDirectory() as d:
            for name, code in (("podman", 0), ("uv", 0)):
                with open(os.path.join(d, name), "w") as f:
                    f.write(f"#!/bin/sh\nexit {code}\n")
                os.chmod(os.path.join(d, name), 0o755)
            out = subprocess.run(["/bin/sh", "-c", hosts.PROBE], env={"PATH": d}, capture_output=True, text=True).stdout
        self.assertEqual([l for l in out.splitlines() if l.split("=")[0] in ("docker", "podman", "docker_ok", "podman_ok")],
                         ["docker=no", "podman=yes", "podman_ok=yes"])

    def test_a_refusal_names_its_reason(self):
        denied = hosts.test(hosts.parse("bench"), run=lambda a, t: (255, "", "ubuntu@10.0.0.4: Permission denied (publickey).\n"))
        self.assertFalse(denied["ok"]); self.assertEqual(denied["error"], "refused: no usable key")
        unknown = hosts.test(hosts.parse("nowhere"), run=lambda a, t: (255, "", "ssh: Could not resolve hostname nowhere: Name or service not known\n"))
        self.assertEqual(unknown["error"], "the host name does not resolve")
        slow = hosts.test(hosts.parse("bench"), run=lambda a, t: (124, "", "no answer within 12 s"))
        self.assertEqual(slow["error"], "no answer within 12 s")
        odd = hosts.test(hosts.parse("bench"), run=lambda a, t: (0, "something else\n", ""))
        self.assertFalse(odd["ok"])                                                     # a shell that did not run the probe


class KeysOntoHosts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nullius-keys-")); self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_key_file_is_kept_where_only_ssh_reads_it(self):
        path = hosts.save_key("ubuntu", "10.0.0.4", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", key_dir=str(self.tmp / "k"))
        self.assertEqual(path, str(self.tmp / "k" / "ubuntu@10.0.0.4"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600"); self.assertEqual(oct(os.stat(self.tmp / "k").st_mode & 0o777), "0o700")
        self.assertTrue(Path(path).read_bytes().endswith(b"-----\n"))
        for bad in (b"ssh-ed25519 AAAA public", b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nx", b"x" * 20001):
            with self.assertRaises(ValueError):
                hosts.save_key("u", "h", bad, key_dir=str(self.tmp / "k"))
        self.assertEqual(hosts.parse(f"ubuntu@10.0.0.4:22:{path}").key, path)

    def test_a_host_removed_from_the_list_takes_only_the_key_saved_for_it(self):
        k = self.tmp / "k"
        saved = [hosts.save_key("u", h, b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", key_dir=str(k)) for h in ("a", "b", "c")]
        for name in ("id_ed25519", "id_ed25519.pub"):
            (k / name).write_text(name)
        mine = self.tmp / "u@d"; mine.write_text("mine")
        old = f"u@a:{saved[0]}, u@b:{saved[1]}, Lab=u@b:2222:{saved[1]}, u@c:{saved[2]}, u@e:{k / 'id_ed25519'}, u@d:{mine}, u@f:{saved[2]}, bench"
        self.assertEqual(hosts.forget_keys(old, f"Lab=u@b:2222:{saved[1]}", key_dir=str(k)), [])
        self.assertEqual(sorted(p.name for p in k.iterdir()), ["id_ed25519", "id_ed25519.pub", "u@b"])   # u@b: still named
        self.assertTrue(mine.exists())                                    # the user's own file, not save_key's
        self.assertEqual(hosts.forget_keys("not a host", "", key_dir=str(k)), [])

    def test_a_key_that_cannot_be_deleted_is_said_and_the_others_still_go(self):
        k = self.tmp / "k"
        a, b = (hosts.save_key("u", h, b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", key_dir=str(k)) for h in ("a", "b"))
        unlink = os.unlink
        def refuse(path):
            if path == a:
                raise PermissionError(13, "Permission denied", path)
            unlink(path)
        with patch.object(os, "unlink", refuse):
            self.assertEqual(hosts.forget_keys(f"u@a:{a}, u@b:{b}", "", key_dir=str(k)), [(a, "Permission denied")])
        self.assertTrue(os.path.exists(a)); self.assertFalse(os.path.exists(b))

    def test_the_own_key_is_made_once(self):
        calls = []
        def keygen(argv, timeout):
            calls.append(argv); Path(argv[-1]).write_text("private"); Path(argv[-1] + ".pub").write_text("ssh-ed25519 AAAA nullius\n"); return 0, "", ""
        path = str(self.tmp / "own" / "id_ed25519")
        self.assertEqual(hosts.own_key(path, run=keygen), path); self.assertEqual(hosts.own_key(path, run=keygen), path)
        self.assertEqual(len(calls), 1); self.assertIn("-N", calls[0]); self.assertEqual(calls[0][calls[0].index("-N") + 1], "")
        with self.assertRaises(RuntimeError):
            hosts.own_key(str(self.tmp / "no" / "key"), run=lambda a, t: (1, "", "ssh-keygen: not here"))

    def test_default_keys_follow_the_profile_at_use_time(self):
        with patch.dict(os.environ, {"NULLIUS_HOME": str(self.tmp / "profile")}):
            path = hosts.save_key("user", "host", b"-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n")
            self.assertEqual(path, str(self.tmp / "profile/config/ssh/user@host"))
            def keygen(argv, timeout):
                Path(argv[-1]).write_text("fixture")
                return 0, "", ""
            self.assertEqual(hosts.own_key(run=keygen), str(self.tmp / "profile/config/ssh/id_ed25519"))

    def test_the_password_goes_to_ssh_once_and_the_entry_names_the_key(self):
        key = self.tmp / "id_ed25519"; key.write_text("private"); (self.tmp / "id_ed25519.pub").write_text("ssh-ed25519 AAAA nullius\n")
        seen = []
        def fake_pty(argv, answer):
            seen.append((argv, answer)); return 0, "enrolled\n"
        h = hosts.enrol("ubuntu", "10.0.0.4", 2222, "hunter2", key=str(key), run_pty=fake_pty)
        self.assertEqual(h.entry, f"ubuntu@10.0.0.4:2222:{key}"); self.assertEqual(h.key, str(key))
        argv, answer = seen[0]
        self.assertEqual(answer, "hunter2"); self.assertIn("PubkeyAuthentication=no", argv); self.assertIn("NumberOfPasswordPrompts=1", argv)
        self.assertIn("ssh-ed25519 AAAA nullius", argv[-1]); self.assertIn("authorized_keys", argv[-1]); self.assertNotIn("hunter2", " ".join(argv))
        with self.assertRaises(RuntimeError) as cm:
            hosts.enrol("ubuntu", "10.0.0.4", None, "wrong", key=str(key), run_pty=lambda a, p: (255, "Permission denied, please try again.\n"))
        self.assertEqual(str(cm.exception), "the password was not accepted")

    def test_the_pty_runner_answers_the_prompt_and_keeps_nothing_typed(self):
        prog = self.tmp / "prompt.py"
        prog.write_text("import sys\nsys.stdout.write('Password: '); sys.stdout.flush()\nline = sys.stdin.readline().strip()\nprint('enrolled' if line == 's3cret' else 'Permission denied')\nsys.exit(0 if line == 's3cret' else 255)\n")
        import sys
        rc, out = hosts.pty_run([sys.executable, str(prog)], "s3cret", timeout=10)
        self.assertEqual(rc, 0); self.assertIn("enrolled", out); self.assertNotIn("s3cret", out)
        rc, out = hosts.pty_run([sys.executable, str(prog)], "nope", timeout=10)
        self.assertEqual(rc, 255); self.assertIn("Permission denied", out)


if __name__ == "__main__":
    unittest.main()
