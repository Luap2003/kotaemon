# main.py
import uuid
import hashlib
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from sqlmodel import SQLModel, Field, Session, create_engine, select

# --- Models ---------------------------------------------------------------

class UserBase(SQLModel):
    username: str
    admin: bool = False

class User(UserBase, table=True):
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True,
    )
    username_lower: str = Field(index=True, unique=True)
    password: str  # stored as SHA256 hex digest

class UserCreate(SQLModel):
    username: str
    password: str
    admin: Optional[bool] = False

class UserRead(SQLModel):
    id: str
    username: str
    admin: bool

# --- Database setup ------------------------------------------------------

DATABASE_URL = "sqlite:////home/user/Documents/arbeit/kotaemon/ktem_app_data/user_data/sql.db"
engine = create_engine(DATABASE_URL, echo=True)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

# --- Application ---------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# --- Routes --------------------------------------------------------------

@app.post(
    "/users/",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user",
)
def create_user(user_in: UserCreate):
    user_in_lower = user_in.username.lower()
    hashed = hashlib.sha256(user_in.password.encode()).hexdigest()

    with Session(engine) as session:
        # Check if already exists
        statement = select(User).where(User.username_lower == user_in_lower)
        existing = session.exec(statement).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Username '{user_in.username}' already exists."
            )

        user = User(
            username=user_in.username,
            username_lower=user_in_lower,
            password=hashed,
            admin=user_in.admin,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    return UserRead.from_orm(user)
