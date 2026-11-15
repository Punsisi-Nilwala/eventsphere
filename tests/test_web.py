import re

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import mk_user


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, email, password="Passw0rd!"):
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"email": email, "password": password, "csrf_token": token},
                       follow_redirects=False)


def test_passwords_are_hashed(db):
    u = mk_user(db)
    assert u.password_hash.startswith("pbkdf2$") and "Passw0rd!" not in u.password_hash


def test_post_without_csrf_token_is_forbidden(db):
    client = TestClient(app)
    r = client.post("/login", data={"email": "x@y.lk", "password": "nope"})
    assert r.status_code == 403


def test_role_checks_on_admin_and_scanner(db):
    attendee = mk_user(db, role="attendee")
    staff = mk_user(db, role="staff")
    admin = mk_user(db, role="admin")
    anon = TestClient(app)
    assert anon.get("/admin", follow_redirects=False).status_code == 303

    c1 = TestClient(app)
    assert login(c1, attendee.email).status_code == 303
    assert c1.get("/admin").status_code == 403
    assert c1.get("/staff/scan").status_code == 403

    c2 = TestClient(app)
    login(c2, staff.email)
    assert c2.get("/staff/scan").status_code == 200
    assert c2.get("/admin").status_code == 403

    c3 = TestClient(app)
    login(c3, admin.email)
    assert c3.get("/admin").status_code == 200
    assert c3.get("/admin/messages").status_code == 200
