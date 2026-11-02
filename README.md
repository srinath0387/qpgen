# ML Lab Exam: Question Paper Generator

Students open the home page, see the datasets, and enter **registration number + PIN** to get their questions.
Questions are stored on the server and only sent after a valid login.

## Run

```bash
pip install -r requirements.txt
set ADMIN_PASSWORD=choose-a-password        # Windows (PowerShell: $env:ADMIN_PASSWORD="...")
export ADMIN_PASSWORD=choose-a-password     # Linux / macOS
python app.py                               # development
# or, for a full class: waitress-serve --port=5000 app:app
```

- Students: `http://<lab-pc-ip>:5000/`
- Admin: `http://<lab-pc-ip>:5000/admin`
- First run prints a random exam PIN in the console; change it in the admin page.

## How it works

1. **Slots**: 1A, 1B, 2A, 2B... (use "Quick generate" or add your own). Each student gets one question from every slot.
2. **Questions**: add questions to each slot (several at once with `---` between them).
3. **Fixed questions**: choose a question and list the reg numbers that must get it.
4. **Everyone else** gets a random question from the slot's pool on first login. It is then locked, so refreshing or logging in again shows the same paper.
5. **Roster** (optional): if filled, only listed reg numbers can log in.
6. **Datasets**: upload files in the admin page; they appear on the home page for everybody.
7. **Who got what**: see the table in the admin page or download the CSV.
8. **Output snapshots**: after logging in, each student can upload screenshots (PNG, JPG, GIF, WEBP or PDF, 10 MB each, 15 per part) for every question part. In the admin page, open section 8 to view them, or download everything as a ZIP (one folder per reg number). Untick "Accept output snapshot uploads" in settings to close uploads.

## Notes

- Deleting a question that was already given out means affected students get a new random one on their next page load. Avoid doing that during the exam.
- Data lives in `exam.db` and `uploads/`. Delete both to start fresh.
- Everyone shares one PIN, so anyone with it plus a roster reg number can open that student's paper. Keep the PIN off the board until the exam starts and use the "Exam is open" switch.
