from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List

class TransactionBase(BaseModel):
    amount: float
    description: str
    category: Optional[str] = None

class TransactionCreate(TransactionBase):
    pass

class Transaction(TransactionBase):
    id: int
    user_id: int
    date: datetime

    class Config:
        from_attributes = True

class UserBase(BaseModel):
    phone_number: str
    name: Optional[str] = None

class UserCreate(UserBase):
    pass

class User(UserBase):
    id: int
    created_at: datetime
    transactions: List[Transaction] = []

    class Config:
        from_attributes = True
