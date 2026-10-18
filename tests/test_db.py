

def test_one_connection_is_safe_to_share_between_threads(tmp_path):
    # live 2026-10-18: poll loop + main thread on one connection → sqlite3.InterfaceError "bad parameter or other API misuse"
    import threading
    from codepilot.db import connect
    from codepilot.runs import RunStore
    conn = connect(tmp_path / "cp.db"); runs = RunStore(conn); errors = []

    def work(n):
        try:
            for i in range(150):
                runs.claim("o/r", "issue", n * 1000 + i, "t")
                list(conn.execute("select id, state from runs where repo=? limit 50", ("o/r",)))
                conn.execute("select count(*) from runs").fetchone()
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == [] and conn.execute("select count(*) from runs").fetchone()[0] == 8 * 150
