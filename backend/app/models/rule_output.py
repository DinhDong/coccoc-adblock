from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.mysql import JSON, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RuleOutput(Base):
    __tablename__ = "rule_outputs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    input_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_inputs.id"),
        nullable=False,
        index=True,
    )
    rules: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    validation_result: Mapped[dict | None] = mapped_column(JSON)
    after_screenshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[object] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    crawl_input: Mapped["CrawlInput"] = relationship(back_populates="rule_outputs")
