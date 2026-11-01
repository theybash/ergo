"""Delegates: nine at most, none of their own, and never a way to the owner except their parent."""

import json

from tests.harness import Case

from commandcenter import delegates, registry


class DelegateTests(Case):
    def test_the_rules(self):
        home = self.dir / "acme"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps(
            {"env": {"CREDENTIAL_SIDE": "work"}, "permissions": {"deny": ["Bash(gh pr merge:*)"]}}))
        registry.create(name="acme", cwd=str(home))

        made = [delegates.make("acme", f"topic {n}", "one topic") for n in range(9)]
        one = registry.get(made[0]["id"])
        assert one["parent"] == "acme" and one["cwd"] == str(home / "delegates" / "topic-0")
        copied = json.loads((home / "delegates" / "topic-0" / ".claude" / "settings.json").read_text())
        assert copied["env"]["CREDENTIAL_SIDE"] == "work"
        assert "Bash(gh pr merge:*)" in copied["permissions"]["deny"]

        with self.assertRaises(OverflowError):
            delegates.make("acme", "tenth", "one too many")
        delegates.archive("acme", made[0]["id"])
        delegates.make("acme", "tenth", "fits now")
        with self.assertRaises(OverflowError):
            delegates.revive("acme", made[0]["id"])
        with self.assertRaises(PermissionError):
            delegates.make(made[1]["id"], "grandchild", "not allowed")

        assert delegates.refuse("/api/posts", {"agent": made[1]["id"]})
        assert delegates.refuse("/api/agents/x/call", {}, call_of=made[1]["id"])
        assert delegates.refuse("/api/posts", {"agent": "acme"}) is None

        # A delegate finishing its turn rings nothing on the phone.
        from commandcenter import server
        said = server.phone_digest({"agents": [{"id": "acme"}, {"id": "d", "parent": "acme"}]})
        assert list(said["agents"]) == ["acme"]

        delegates.set_hidden("acme", True)
        assert all(d["hidden"] for d in registry.delegates("acme"))
