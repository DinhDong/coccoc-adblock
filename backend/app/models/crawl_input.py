from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text, text
from sqlalchemy.dialects.mysql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CrawlInput(Base):
    __tablename__ = "crawl_inputs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain: Mapped[str | None] = mapped_column(Text)
    domain_type: Mapped[str | None] = mapped_column(String(255))
    jira_ticket_code: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    ad_type: Mapped[str | None] = mapped_column(String(255))
    ticket_context: Mapped[str | None] = mapped_column(Text)
    before_screenshot: Mapped[str | None] = mapped_column(Text)
    crawl_duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text("'new'"),
    )
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

    rule_outputs: Mapped[list["RuleOutput"]] = relationship(
        back_populates="crawl_input",
        cascade="all, delete-orphan",
    )
