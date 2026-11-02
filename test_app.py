import io, os, re, tempfile, shutil, importlib, sys

tmp = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "pw"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A
A.DB_PATH = os.path.join(tmp, "t.db")
A.UPLOADS = os.path.join(tmp, "up"); os.makedirs(A.UPLOADS)
A.init_db()
app = A.app
app.config["TESTING"] = True


def token(c, url="/"):
    html = c.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


adm = app.test_client()
t = token(adm, "/admin/login")
r = adm.post("/admin/login", data={"csrf": t, "password": "pw"})
assert r.status_code == 302, r.status_code
t = token(adm, "/admin")

with app.app_context():
    db = A.get_db()
    pin = A.get_setting("pin")
    slots = {s["label"]: s["id"] for s in A.get_slots(db)}
assert set(slots) == {"1A", "1B", "2A", "2B"}

for lab in slots:
    adm.post("/admin/questions/add", data={"csrf": t, "slot_id": slots[lab],
             "text": f"{lab} first\n---\n{lab} second\n---\n{lab} third"})
with app.app_context():
    q1a = [r["id"] for r in A.get_db().execute("SELECT id FROM questions WHERE slot_id=? ORDER BY id", (slots["1A"],))]
adm.post("/admin/fixed/add", data={"csrf": t, "question_id": q1a[2], "regs": "r001, R 002"})
adm.post("/admin/roster", data={"csrf": t, "action": "add", "data": "reg,name\nR001,Anil\nR002,Bhavya\nR003,Chitra"})
adm.post("/admin/datasets/upload", data={"csrf": t, "title": "Iris", "file": (io.BytesIO(b"a,b\n1,2\n"), "iris.csv")},
         content_type="multipart/form-data")

# public home shows dataset, no questions
st = app.test_client()
home = st.get("/").get_data(as_text=True)
assert "Iris" in home and "first" not in home
assert st.get("/paper").status_code == 302          # blocked without login
tk = token(st)
# wrong pin / unlisted reg
assert "Invalid" in st.post("/login", data={"csrf": tk, "reg_no": "R001", "pin": "bad"}, follow_redirects=True).get_data(as_text=True)
assert "not in the exam list" in st.post("/login", data={"csrf": tk, "reg_no": "ZZZ", "pin": pin}, follow_redirects=True).get_data(as_text=True)
# no csrf -> 400
assert st.post("/login", data={"reg_no": "R001", "pin": pin}).status_code == 400

# fixed student
p = st.post("/login", data={"csrf": tk, "reg_no": "r001", "pin": pin}, follow_redirects=True).get_data(as_text=True)
assert "1A third" in p and "Q1B" in p and "Q2A" in p and "Q2B" in p, p
# random student gets locked paper
s2 = app.test_client(); tk2 = token(s2)
first = s2.post("/login", data={"csrf": tk2, "reg_no": "R003", "pin": pin}, follow_redirects=True).get_data(as_text=True)
body = lambda h: re.findall(r'class="qtext">(.*?)</div>', h, re.S)
b1 = body(first); assert len(b1) == 4
for _ in range(5):
    assert body(s2.get("/paper").get_data(as_text=True)) == b1
s3 = app.test_client(); tk3 = token(s3)
again = s3.post("/login", data={"csrf": tk3, "reg_no": "R003", "pin": pin}, follow_redirects=True).get_data(as_text=True)
assert body(again) == b1

# admin pages + csv + download
assert "R003" in adm.get("/admin").get_data(as_text=True)
assert "R003" in adm.get("/admin/assignments.csv").get_data(as_text=True)
assert st.get("/datasets/1/download").data == b"a,b\n1,2\n"
assert app.test_client().get("/admin").status_code == 302

# ---- output snapshot uploads
import zipfile
A.SUBMISSIONS = os.path.join(tmp, "subs"); os.makedirs(A.SUBMISSIONS)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
tku = token(st, "/paper")
def up(client, tok, slot, name, data):
    return client.post("/submit", data={"csrf": tok, "slot_id": slot, "files": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
assert "Uploaded 1 file" in up(st, tku, slots["1A"], "out1.png", PNG)
assert "only PNG" in up(st, tku, slots["1A"], "evil.html", b"<script>1</script>")
assert "only PNG" in up(st, tku, slots["1A"], "fake.png", b"<script>not a png</script>")
paper_html = st.get("/paper").get_data(as_text=True)
sid = int(re.search(r"/submissions/(\d+)/file", paper_html).group(1))
r = st.get(f"/submissions/{sid}/file"); assert r.status_code == 200 and r.data == PNG
assert r.headers["X-Content-Type-Options"] == "nosniff"
# another student cannot read it; logged-out cannot either; no csrf -> 400
assert s2.get(f"/submissions/{sid}/file").status_code == 404
assert app.test_client().get(f"/submissions/{sid}/file").status_code == 404
assert st.post("/submit", data={"slot_id": slots["1A"]}).status_code == 400
# admin sees it, zip contains it under reg folder
assert "R001" in adm.get("/admin").get_data(as_text=True)
assert adm.get(f"/admin/submissions/{sid}/file").data == PNG
z = zipfile.ZipFile(io.BytesIO(adm.get("/admin/submissions.zip").data))
assert any(n.startswith("R001/1A_") and n.endswith("out1.png") for n in z.namelist()), z.namelist()
assert app.test_client().get("/admin/submissions.zip").status_code == 302
# student removes own file; closing uploads blocks upload
st.post(f"/submissions/{sid}/delete", data={"csrf": tku})
assert "out1.png" not in st.get("/paper").get_data(as_text=True)
adm.post("/admin/settings", data={"csrf": t, "title": "X", "pin": pin, "instructions": "", "open": "on"})  # submit_open off
assert "closed" in up(st, tku, slots["1A"], "late.png", PNG)

# exam closed
adm.post("/admin/settings", data={"csrf": t, "title": "X", "pin": pin, "instructions": ""})
assert "not open" in app.test_client().get("/").get_data(as_text=True)
print("ALL TESTS PASSED")
shutil.rmtree(tmp)
