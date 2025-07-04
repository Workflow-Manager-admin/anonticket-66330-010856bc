"""
FastAPI backend for an anonymous ticketing system.
- Allows anyone to submit and reply to tickets, with all actions anonymous.
- Provides endpoints for: list tickets, create ticket, ticket details (with replies), submit reply.
- All data is stored in a database (integrate via environment variable config).
- No sensitive information or user IDs are stored.
"""

from fastapi import FastAPI, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.future import select
import os
import datetime

# ---- Database Setup ----

Base = declarative_base()


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    replies = relationship("Reply", back_populates="ticket", cascade="all, delete-orphan")


class Reply(Base):
    __tablename__ = "replies"
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    ticket = relationship("Ticket", back_populates="replies")


# Environment variable for DB URL, must be async-supported (sqlite+aiosqlite, postgresql+asyncpg, etc)
DB_URL = os.getenv("TICKETING_DB_URL", "sqlite+aiosqlite:///./ticketing.db")

engine = create_async_engine(DB_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with SessionLocal() as session:
        yield session


# ---- Pydantic Models ----

class ReplyBase(BaseModel):
    content: str = Field(..., description="Content of the reply")


class ReplyCreate(ReplyBase):
    pass


class ReplyInDB(ReplyBase):
    id: int
    created_at: datetime.datetime

    class Config:
        orm_mode = True


class TicketBase(BaseModel):
    title: str = Field(..., description="Title of the ticket")
    content: str = Field(..., description="Content/body of the ticket")


class TicketCreate(TicketBase):
    pass


class TicketInDB(TicketBase):
    id: int
    created_at: datetime.datetime

    class Config:
        orm_mode = True


class TicketWithReplies(TicketInDB):
    replies: List[ReplyInDB] = []


# ---- FastAPI App and Middleware ----

app = FastAPI(
    title="Anonymous Ticketing System API",
    version="1.0.0",
    description="Backend for a fun and fully anonymous ticketing system. No authentication, no user tracking!"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

openapi_tags = [
    {"name": "Tickets", "description": "Anonymous ticket creation, listing, and detail"},
    {"name": "Replies", "description": "Anonymous replies to tickets"}
]

# ---- API Endpoints ----

@app.get("/", tags=["Tickets"])
def health_check():
    """
    Health check endpoint.
    """
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.post("/tickets/", response_model=TicketInDB, status_code=status.HTTP_201_CREATED, tags=["Tickets"], summary="Create new anonymous ticket", description="Create (submit) a new anonymous ticket.")
async def create_ticket(ticket: TicketCreate, db: AsyncSession = Depends(get_db)):
    """
    Create a new anonymous ticket.

    Parameters:
    - ticket: TicketCreate (title, content)

    Returns:
    - Created ticket data (id, title, content, created_at)
    """
    new_ticket = Ticket(
        title=ticket.title,
        content=ticket.content,
        created_at=datetime.datetime.utcnow()
    )
    db.add(new_ticket)
    await db.commit()
    await db.refresh(new_ticket)
    return new_ticket


# PUBLIC_INTERFACE
@app.get("/tickets/", response_model=List[TicketInDB], tags=["Tickets"], summary="Get all tickets", description="Retrieve all tickets in the system (no authentication required).")
async def list_tickets(db: AsyncSession = Depends(get_db)):
    """
    Get all tickets, sorted by most recent first.

    Returns:
    - List of ticket summaries.
    """
    result = await db.execute(
        select(Ticket).order_by(Ticket.created_at.desc())
    )
    tickets = result.scalars().all()
    return tickets


# PUBLIC_INTERFACE
@app.get("/tickets/{ticket_id}/", response_model=TicketWithReplies, tags=["Tickets"], summary="Ticket details (with replies)", description="Get all details and replies for a specific ticket.")
async def ticket_detail(ticket_id: int, db: AsyncSession = Depends(get_db)):
    """
    Get details and replies for a ticket by ID.

    Parameters:
    - ticket_id: integer

    Returns:
    - Full ticket info, including anonymous replies.
    """
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    # Replies are eagerly loaded by relationship defined in SQLAlchemy model
    ticket_dict = TicketWithReplies.from_orm(ticket)
    # Manually load replies as well for serialization
    ticket_dict.replies = [ReplyInDB.from_orm(reply) for reply in sorted(ticket.replies, key=lambda r: r.created_at)]
    return ticket_dict


# PUBLIC_INTERFACE
@app.post("/tickets/{ticket_id}/replies/", response_model=ReplyInDB, status_code=status.HTTP_201_CREATED, tags=["Replies"], summary="Submit reply (anonymous)", description="Submit an anonymous reply to a ticket.")
async def submit_reply(ticket_id: int, reply: ReplyCreate, db: AsyncSession = Depends(get_db)):
    """
    Submit an anonymous reply to a ticket.

    Parameters:
    - ticket_id: integer (the ticket to reply to)
    - reply: ReplyCreate (content)

    Returns:
    - Created reply data (id, content, created_at)
    """
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    new_reply = Reply(
        ticket_id=ticket_id,
        content=reply.content,
        created_at=datetime.datetime.utcnow()
    )
    db.add(new_reply)
    await db.commit()
    await db.refresh(new_reply)
    return new_reply


@app.on_event("startup")
async def startup_event():
    """
    Ensures the database is initialized with the latest schema at app startup.

    This auto-creates tables if they don't exist. 
    (Good for SQLite/local use; use Alembic for production migrations with explicit DDL.)
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
