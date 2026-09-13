from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Short = Annotated[str, Field(min_length=1, max_length=200)]
Body = Annotated[str, Field(min_length=1, max_length=3000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductIn(Input):
    name: Short
    description: Annotated[str, Field(min_length=1, max_length=10000)]
    category: Annotated[str, Field(min_length=1, max_length=100)]
    price: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
    stock: Annotated[int, Field(ge=0)]
    specifications: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ProductPatch(Input):
    name: Short | None = None
    description: Annotated[str, Field(min_length=1, max_length=10000)] | None = None
    category: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    price: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)] | None = None
    stock: Annotated[int, Field(ge=0)] | None = None
    specifications: dict[str, str | int | float | bool] | None = None


class FAQIn(Input):
    question: Annotated[str, Field(min_length=1, max_length=500)]
    answer: Annotated[str, Field(min_length=1, max_length=10000)]
    category: Annotated[str, Field(min_length=1, max_length=100)]


class FAQPatch(Input):
    question: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    answer: Annotated[str, Field(min_length=1, max_length=10000)] | None = None
    category: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class KnowledgeIn(Input):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    content: Annotated[str, Field(min_length=1, max_length=100000)]
    source: Annotated[str, Field(min_length=1, max_length=500)]


class ChatIn(Input):
    telegram_id: Annotated[int, Field(gt=0)]
    event_id: Annotated[str, Field(min_length=1, max_length=100)]
    username: Annotated[str, Field(max_length=128)] | None = None
    first_name: Annotated[str, Field(max_length=128)] | None = None
    text: Body


class TextIn(Input):
    text: Body


class FeedbackIn(Input):
    telegram_id: Annotated[int, Field(gt=0)]
    message_id: Annotated[int, Field(gt=0)]
    helpful: bool
