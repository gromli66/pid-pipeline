import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
engine = create_engine(os.getenv("DATABASE_URL"))
s = Session(engine)
s.execute(text("UPDATE diagrams SET status='validated_junctions', error_message=NULL, error_stage=NULL WHERE uid='c8c5a0da-25ca-4ce9-9ddd-56d05b60af96'"))
s.commit()
print("OK")
