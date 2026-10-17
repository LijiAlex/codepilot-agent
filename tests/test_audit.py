# tests/test_audit.py
import sqlite3, pytest
from codepilot.db import connect
from codepilot.audit import AuditLog, redact

def test_redacts_secret_shapes():
    s = "token ghs_ABCDEFGHIJKLMNOPQRSTUVWX123456 key sk-ant-api03-xyzxyzxyzxyzxyz AKIAABCDEFGHIJKLMNOP Bearer abc.def.ghi"
    out = redact(s)
    for leaked in ("ghs_ABCD", "sk-ant-api03", "AKIAABCDEFGHIJKLMNOP", "abc.def.ghi"):
        assert leaked not in out
    assert "[REDACTED]" in out

def test_audit_is_append_only(tmp_path):
    conn = connect(tmp_path / "cp.db")
    log = AuditLog(conn)
    log.log("liji", "approve", target="#12", detail={"note": "Bearer zzz.yyy.xxx"})
    row = conn.execute("select * from events").fetchone()
    assert "zzz.yyy.xxx" not in row["detail_json"]
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("update events set action='x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("delete from events")


def test_redacts_daytona_and_langsmith_keys():
    from codepilot.audit import redact
    out = redact("dtn_" + "a" * 30 + " lsv2_pt_" + "b" * 30)
    assert "aaaa" not in out and "bbbb" not in out
