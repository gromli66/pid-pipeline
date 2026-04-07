import psycopg2
conn = psycopg2.connect("postgresql://pid_user:changeme@localhost:5433/pid_pipeline")
conn.autocommit = True
cur = conn.cursor()
cur.execute("UPDATE alembic_version SET version_num = 'd4e5f6a7b8c9'")
print("stamped back to d4e5f6a7b8c9")
conn.close()
