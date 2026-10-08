# D'Tale Exam Portal (Flask + PostgreSQL)

Teacher uploads a .txt exam paper. Students enter their details, take the timed exam,
then upload a PDF answer sheet. The teacher logs in with a PIN and downloads every PDF.

## Run locally
    pip install -r requirements.txt
    python app.py          # http://127.0.0.1:5000  (default PIN: dtale123, uses SQLite)

## Deploy on Render
1. Push this folder to a GitHub repo (app.py, requirements.txt, render.yaml, static/ at the repo root).
2. Render dashboard: New > Blueprint > select the repo > Apply.
   (Or create a PostgreSQL database and a Web Service by hand.)
3. When asked, set TEACHER_PIN to your own PIN.
4. Open the onrender.com URL. Teacher tab: log in, publish the exam. Share the same URL with students.

Manual setup instead of Blueprint:
- Web Service: Build `pip install -r requirements.txt`, Start `gunicorn app:app --workers 2 --timeout 60`
- Environment: DATABASE_URL (Internal Database URL of your Render Postgres), SECRET_KEY (any long random string), TEACHER_PIN

Optional env: UPLOAD_GRACE_MIN (default 30) = minutes students have to upload after the exam ends.
