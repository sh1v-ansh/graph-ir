"""Create a demo repo: a small app, then an AI-style change on top of it.

    python examples/make_demo.py /path/to/demo
    cd /path/to/demo && gir --task "add email verification to signup"
"""
import subprocess
import sys
import textwrap
from pathlib import Path

BASE = {
    "app/__init__.py": "",
    "app/db.py": '''
        import sqlite3

        _conn = sqlite3.connect(":memory:")


        def query(sql, *args):
            return _conn.execute(sql, args).fetchall()


        def insert(table, **row):
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            _conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            _conn.commit()
    ''',
    "app/mailer.py": '''
        import smtplib


        def send(to, subject, body):
            with smtplib.SMTP("localhost") as s:
                s.sendmail("noreply@example.com", [to], f"Subject: {subject}\\n\\n{body}")
    ''',
    "app/users.py": '''
        from app import db


        class User:
            def __init__(self, email, verified=False):
                self.email = email
                self.verified = verified

            def save(self):
                db.insert("users", email=self.email, verified=self.verified)


        def signup(email):
            """Create a user."""
            user = User(email)
            user.save()
            return user
    ''',
    "app/routes.py": '''
        from app.users import signup


        def post_signup(request):
            user = signup(request["email"])
            return {"ok": True, "email": user.email}
    ''',
    "tests/test_users.py": '''
        from app.users import signup


        def test_signup():
            assert signup("a@b.c").email == "a@b.c"
    ''',
}

# What the coding agent wrote for: "add email verification to signup"
CHANGE = {
    "app/users.py": '''
        import secrets
        import time

        from app import db, mailer


        class User:
            def __init__(self, email, verified=False):
                self.email = email
                self.verified = verified

            def save(self):
                db.insert("users", email=self.email.lower(), verified=self.verified)
                self.log_change("saved")


        def signup(email):
            """Create a user and send them a verification link."""
            user = User(email)
            user.save()
            send_verification(user)
            return user


        def send_verification(user):
            token = secrets.token_urlsafe(16)
            db.insert("tokens", email=user.email, token=token, created=time.time())
            mailer.send_async(user.email, "Verify your email", f"/verify?token={token}")


        def verify_email(token):
            try:
                rows = db.query("SELECT email, created FROM tokens WHERE token = ?", token)
                email, created = rows[0]
                if time.time() - created > 86400:
                    return False
                db.query("UPDATE users SET verified = 1 WHERE email = ?", email)
                return True
            except Exception:
                pass
    ''',
    "app/routes.py": '''
        from app.users import signup, verify_email


        def post_signup(request):
            user = signup(request["email"])
            return {"ok": True, "email": user.email}


        def get_verify(request):
            ok = verify_email(request["token"])
            return {"verified": ok}
    ''',
    "app/db.py": '''
        import sqlite3

        _conn = sqlite3.connect(":memory:")


        def query(sql, *args):
            """Run a query and return all rows."""
            return _conn.execute(sql, args).fetchall()


        def insert(table, **row):
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            _conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())
            )
            _conn.commit()
    ''',
}


def write(root: Path, files: dict):
    for path, src in files.items():
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).lstrip("\n"))


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "demo@example.com")
    git(root, "config", "user.name", "demo")
    write(root, BASE)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base app")
    write(root, CHANGE)


if __name__ == "__main__":
    make(Path(sys.argv[1]))
    print(f"demo repo ready at {sys.argv[1]} (change is uncommitted)")
