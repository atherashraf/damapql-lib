# app/damapql/config_registry/workflows/models.py
from datetime import datetime
from typing import Optional
import sqlalchemy as db
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.damapql.utils.adapters.db import DBBase
from app.damapql.auth.models import User


class WorkflowInstance(DBBase):
    __tablename__ = "wf_instance"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, nullable=False)
    workflow_key: Mapped[str] = mapped_column(db.String(100), index=True, nullable=False)

    # Generic entity association
    entity_type: Mapped[str] = mapped_column(db.String(100), index=True, nullable=False)
    entity_id: Mapped[int] = mapped_column(db.BigInteger, index=True, nullable=False)

    current_state: Mapped[str] = mapped_column(db.String(100), nullable=False)

    # Spatial unit scopes (matching UserInfo / Organization / Department)
    dist_id: Mapped[Optional[int]] = mapped_column(db.Integer, nullable=True, index=True)
    subdiv_id: Mapped[Optional[int]] = mapped_column(db.Integer, nullable=True, index=True)

    created_by_id: Mapped[int] = mapped_column(db.ForeignKey(f"{User.__tablename__}.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    created_by = relationship(User, foreign_keys=[created_by_id])
    history = relationship(
        "WorkflowAuditLog",
        back_populates="instance",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowAuditLog.timestamp)",
    )


class WorkflowAuditLog(DBBase):
    __tablename__ = "wf_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, nullable=False)
    instance_id: Mapped[int] = mapped_column(db.ForeignKey("wf_instance.id", ondelete="CASCADE"), nullable=False,
                                             index=True)

    from_state: Mapped[str] = mapped_column(db.String(100), nullable=False)
    to_state: Mapped[str] = mapped_column(db.String(100), nullable=False)
    action: Mapped[str] = mapped_column(db.String(100), nullable=False)

    performed_by_id: Mapped[int] = mapped_column(db.ForeignKey(f"{User.__tablename__}.id"), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(db.Text, nullable=True)
    extra_payload: Mapped[Optional[dict]] = mapped_column(db.JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(db.DateTime, default=datetime.utcnow, nullable=False)

    instance = relationship("WorkflowInstance", back_populates="history")
    performed_by = relationship(User, foreign_keys=[performed_by_id])